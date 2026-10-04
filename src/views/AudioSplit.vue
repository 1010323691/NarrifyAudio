<script setup lang="ts">
import { onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useToast } from '@/components/ui/toast'
import { cutAudio, detectSilences, exportAudio, planAudio, probeAudio, zipAudio } from '@/api/audio'
import { cancelDurableTask, findActiveDurableTask, getDurableTask, listDurableTasks } from '@/api/durableTasks'
import { useDurableTaskWait } from '@/composables/useDurableTaskWait'
import type { DurableTask } from '@/api/durableTasks'
import { downloadFile } from '@/utils/fileops'
import { formatBytes, formatDuration } from '@/utils/format'
import type {
  AudioCutResult,
  AudioProbeResult,
  AudioSegment,
  AudioSilencesResult,
  DirListResult,
} from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Input from '@/components/ui/Input.vue'
import Label from '@/components/ui/Label.vue'
import Switch from '@/components/ui/Switch.vue'
import WorkbenchStatus from '@/components/ui/WorkbenchStatus.vue'
import Alert from '@/components/ui/Alert.vue'
import Progress from '@/components/ui/Progress.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import Table from '@/components/ui/Table.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableRow from '@/components/ui/TableRow.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableCell from '@/components/ui/TableCell.vue'
import WorkspaceEntryPicker from '@/components/WorkspaceEntryPicker.vue'
import {
  AudioLines,
  Scissors,
  Wand2,
  Download,
  Loader2,
  XCircle,
  Package,
  FolderOutput,
} from 'lucide-vue-next'

const settings = useSettingsStore()
const projectStore = useProjectStore()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()
const waitForTask = useDurableTaskWait()

async function resolveAudioTask(response: { task_id: string } | Record<string, any>): Promise<Record<string, any>> {
  if (!('task_id' in response)) return response
  const task = await waitForTask.wait(response.task_id)
  if (task.status !== 'succeeded') throw new Error(task.error_message || '任务执行失败')
  return task.result ?? {}
}

const file = ref<{ path: string; name: string } | null>(null)
const probe = ref<AudioProbeResult | null>(null)

// The 06_audio_merge directory the picker scans: its absolute path (from the scan)
// + the selected file name combine into the file the pipeline operates on.
const selectedName = ref('')
const dirPath = ref('')

// Form fields (seeded from config.audio on load).
const targetDuration = ref('10:00')
const smartAlign = ref(true)
const tolerance = ref(15)
const namingFormat = ref('第 {} 集')
const startNumber = ref('1')

const busyProbe = ref(false)
const busyPlan = ref(false)
const busyCut = ref(false)
const busyZip = ref(false)
const busyExport = ref(false)
const error = ref('')

interface Plan {
  segments: AudioSegment[]
  count: number
  aligned: boolean
  snapped: number
  fallbacks: number
}
const plan = ref<Plan | null>(null)
const cutResult = ref<AudioCutResult | null>(null)

// Live task ids (progress shown inline in this view).
const planTaskId = ref<string | null>(null)
const cutTaskId = ref<string | null>(null)
const planTask = ref<DurableTask | null>(null)
const cutTask = ref<DurableTask | null>(null)
let trackingController = new AbortController()
onDeactivated(() => {
  trackingController.abort()
  busyPlan.value = false
  busyCut.value = false
})
onUnmounted(() => trackingController.abort())
onActivated(() => {
  if (trackingController.signal.aborted) {
    trackingController = new AbortController()
    reattachTasks()
  }
})

watch(() => projectStore.activeProjectId, (projectId, previousProjectId) => {
  if (projectId === previousProjectId) return
  trackingController.abort()
  trackingController = new AbortController()
  planTaskId.value = null
  cutTaskId.value = null
  planTask.value = null
  cutTask.value = null
  selectedName.value = ''
  dirPath.value = ''
  file.value = null
  probe.value = null
  plan.value = null
  cutResult.value = null
  error.value = ''
  busyProbe.value = false
  busyPlan.value = false
  busyCut.value = false
  busyZip.value = false
  busyExport.value = false
  if (projectId) reattachTasks()
})

// 刷新恢复：页面重载后本地 taskId 丢失，但后端持久化任务仍在跑。
// 按持久化 task_type 区分两种在途任务重新挂接。
function reattachTasks() {
  const projectId = projectStore.activeProjectId
  if (!projectId) return
  const signal = trackingController.signal
  void listDurableTasks().then((tasks) => {
    if (signal.aborted || projectStore.activeProjectId !== projectId) return
    const planning = findActiveDurableTask(tasks, projectId, 'audio.silences')
    const cutting = findActiveDurableTask(tasks, projectId, 'audio.cut')
    if (planning) {
      planTaskId.value = planning.id
      planTask.value = planning
      busyPlan.value = true
      void trackTask(planning.id, 'plan')
    }
    if (cutting) {
      cutTaskId.value = cutting.id
      cutTask.value = cutting
      busyCut.value = true
      void trackTask(cutting.id, 'cut')
    }
  }).catch(() => undefined)
}

async function trackTask(id: string, kind: 'plan' | 'cut') {
  const projectId = projectStore.activeProjectId
  const isCurrentTracking = () => projectId !== null
    && projectStore.activeProjectId === projectId
    && (kind === 'plan' ? planTaskId.value === id : cutTaskId.value === id)
  try {
    await waitForTask.track(id, (task) => {
      // 跟踪目标已变（项目切换 / 新任务）→ 静默停止，不报错误。
      if (!isCurrentTracking()) return false
      if (kind === 'plan') planTask.value = task
      else cutTask.value = task
    })
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    if (!isCurrentTracking()) return
    error.value = e?.message || '持久化任务状态读取失败'
    if (kind === 'plan') {
      busyPlan.value = false
      planTask.value = null
      planTaskId.value = null
    } else {
      busyCut.value = false
      cutTask.value = null
      cutTaskId.value = null
    }
  }
}

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  const a = settings.config?.audio
  if (a) {
    targetDuration.value = a.target_duration
    smartAlign.value = a.smart_align
    tolerance.value = a.align_tolerance
    namingFormat.value = a.naming_format
    startNumber.value = a.start_number
  }
  reattachTasks()
})

// Capture the 06_audio_merge directory's absolute path from each picker scan, so a
// selection can be turned into a full file path (the backend reads by absolute path).
function onScanned(r: DirListResult) {
  dirPath.value = r.path
}

// A selection in the picker sets the file to operate on and probes it.
watch(selectedName, (name) => {
  if (!name || !dirPath.value) return
  file.value = { path: `${dirPath.value}/${name}`, name }
  probe.value = null
  plan.value = null
  cutResult.value = null
  error.value = ''
  doProbe()
})

async function doProbe() {
  if (!file.value) return
  const source = file.value
  const projectId = projectStore.activeProjectId
  busyProbe.value = true
  error.value = ''
  try {
    const result = await probeAudio(source.path)
    if (projectStore.activeProjectId !== projectId || file.value?.path !== source.path) return
    probe.value = result
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    if (projectStore.activeProjectId !== projectId || file.value?.path !== source.path) return
    error.value = e?.message || '无法读取音频'
    probe.value = null
  } finally {
    if (projectStore.activeProjectId === projectId && file.value?.path === source.path) {
      busyProbe.value = false
    }
  }
}

// Remember the card's parameters in the persisted (workspace) config, so the next
// visit starts from what was last used. Fire-and-forget: a save failure must never
// break the action that used the parameters (same pattern as TextFormat / BatchTTS).
function rememberParams() {
  void settings.save({
    audio: {
      target_duration: targetDuration.value,
      smart_align: smartAlign.value,
      align_tolerance: tolerance.value,
      naming_format: namingFormat.value,
      start_number: startNumber.value,
    },
  })
}

async function buildPlan() {
  if (!file.value || busyPlan.value) return
  const projectId = projectStore.activeProjectId
  const signal = trackingController.signal
  if (!projectId) return
  const source = file.value
  const isCurrent = () => !signal.aborted
    && projectStore.activeProjectId === projectId
    && file.value?.path === source.path
  busyPlan.value = true
  error.value = ''
  cutResult.value = null
  try {
    if (smartAlign.value) {
      // Long-running: pause detection + pause-aligned plan (a backend task).
      const { task_id } = await detectSilences(source.path, {
        targetDuration: targetDuration.value,
        alignTolerance: tolerance.value,
      }, signal)
      signal.throwIfAborted()
      if (!isCurrent()) return
      planTaskId.value = task_id
      planTask.value = await getDurableTask(task_id, signal)
      signal.throwIfAborted()
      if (!isCurrent()) return
      void trackTask(task_id, 'plan')
      // Completion is handled by the watcher on planTask.status.
    } else {
      const r = await planAudio(source.path, targetDuration.value)
      signal.throwIfAborted()
      if (!isCurrent()) return
      plan.value = { segments: r.segments, count: r.count, aligned: false, snapped: 0, fallbacks: 0 }
      rememberParams()
    }
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '生成方案失败'
    planTaskId.value = null
    busyPlan.value = false
  } finally {
    if (!isCurrent()) busyPlan.value = false
  }
}

// Capture the aligned plan once the silence-detection task settles.
watch(
  () => planTask.value?.status,
  (st) => {
    const t = planTask.value
    if (!st || !t) return
    busyPlan.value = false
    if (st === 'succeeded') {
      const r = t.result as unknown as AudioSilencesResult
      plan.value = { segments: r.segments, count: r.count, aligned: true, snapped: r.snapped, fallbacks: r.fallbacks }
      planTaskId.value = null
      planTask.value = null
      rememberParams()
      toast({ title: '智能方案已生成', variant: 'success', description: `停顿吸附 ${r.snapped} 处，回退 ${r.fallbacks} 处` })
    } else if (st === 'failed') {
      error.value = t.error_message || '停顿检测失败'
      planTaskId.value = null
      planTask.value = null
    } else if (st === 'cancelled') {
      planTaskId.value = null
      planTask.value = null
    }
  },
)

async function doCut() {
  if (!file.value || !plan.value || busyCut.value) return
  const projectId = projectStore.activeProjectId
  const signal = trackingController.signal
  if (!projectId) return
  const source = file.value
  const segments = plan.value.segments
  const isCurrent = () => !signal.aborted
    && projectStore.activeProjectId === projectId
    && file.value?.path === source.path
  busyCut.value = true
  error.value = ''
  try {
    // Pass the pre-computed segments so the cut matches exactly what was previewed.
    const { task_id } = await cutAudio(source.path, {
      segments,
      namingFormat: namingFormat.value,
      startNumber: startNumber.value,
    }, signal)
    signal.throwIfAborted()
    if (!isCurrent()) return
    cutTaskId.value = task_id
    cutTask.value = await getDurableTask(task_id, signal)
    signal.throwIfAborted()
    if (!isCurrent()) return
    void trackTask(task_id, 'cut')
    // Remember the cut parameters (fire-and-forget; the cut above already carries them).
    rememberParams()
    // Completion is handled by the watcher on cutTask.status.
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '启动切割失败'
    cutTaskId.value = null
    busyCut.value = false
  } finally {
    if (!isCurrent()) busyCut.value = false
  }
}

watch(
  () => cutTask.value?.status,
  (st) => {
    const t = cutTask.value
    if (!st || !t) return
    busyCut.value = false
    if (st === 'succeeded') {
      const r = t.result as unknown as AudioCutResult
      cutResult.value = r
      cutTaskId.value = null
      cutTask.value = null
      toast({ title: '切割完成', variant: 'success', description: `生成 ${r.file_count} 个文件` })
    } else if (st === 'failed') {
      error.value = t.error_message || '切割失败'
      cutTaskId.value = null
      cutTask.value = null
      toast({ title: '切割失败', variant: 'destructive', description: error.value })
    } else if (st === 'cancelled') {
      cutTaskId.value = null
      cutTask.value = null
    }
  },
)

function cancel(id: string) {
  void cancelDurableTask(id)
}

// The cut output, shaped as the packaging / export endpoints expect it.
function cutFilesSpec() {
  return (cutResult.value?.files ?? []).map((f) => ({ name: f.name, path: f.path }))
}

// 打包下载：zip 切割产物，再通过浏览器下载。
async function doZip() {
  if (!cutResult.value || busyZip.value) return
  busyZip.value = true
  error.value = ''
  try {
    const base = (file.value?.name || '').replace(/\.[^.]+$/, '')
    const r = await resolveAudioTask(await zipAudio({ base, files: cutFilesSpec() }))
    // 浏览器：下载 zip（后端回 Content-Disposition: attachment，存到下载目录）。
    download(r.path || r.zip_path)
    toast({ title: '打包已开始下载', variant: 'success', description: `打包 ${r.file_count} 个文件` })
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '打包失败'
    toast({ title: '打包失败', variant: 'destructive', description: error.value })
  } finally {
    busyZip.value = false
  }
}

// 输出到音频源文件夹：把切割产物复制到源音频同级的「分集」文件夹。
async function doExport() {
  if (!file.value || !cutResult.value || busyExport.value) return
  busyExport.value = true
  error.value = ''
  try {
    const r = await resolveAudioTask(await exportAudio(file.value.path, { files: cutFilesSpec() }))
    // 文件已落到磁盘——这本身就是成功。
    toast({ title: '已输出到源文件夹', variant: 'success', description: `${r.file_count} 个文件 → ${r.dest_dir}` })
  } catch (e: any) {
    if (e?.name === 'AbortError') return
    error.value = e?.message || '输出失败'
    toast({ title: '输出失败', variant: 'destructive', description: error.value })
  } finally {
    busyExport.value = false
  }
}

function download(path: string) {
  downloadFile('07_output', path)
}
</script>

<template>
  <div class="viewport-page">
    <header class="page-header mb-5">
      <div>
        <p class="eyebrow">Pipeline · Split</p>
        <h1 class="page-title">音频分集</h1>
        <p class="page-description">将合并后的有声书切分为多集，可按停顿对齐切点。</p>
      </div>
    </header>

    <div class="page-region" role="region" aria-label="页面工作区" tabindex="0">
      <ProjectGateAlert />

      <!-- 选择文件 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2">
            <AudioLines class="h-5 w-5" />选择音频
          </CardTitle>
        </CardHeader>
        <CardContent class="space-y-3">
          <WorkspaceEntryPicker
            module="06_audio_merge"
            :extensions="['mp3', 'wav', 'm4a', 'aac', 'ogg', 'oga', 'opus', 'flac', 'webm']"
            :show-default="false"
            v-model="selectedName"
            label="待分集音频"
            empty-hint="暂无可分集的音频，请先到「音频合并」生成音频。"
            @scanned="onScanned"
          />
          <div v-if="file" class="flex flex-wrap items-center gap-3 rounded-md bg-muted/50 px-3 py-2 text-sm">
            <span class="font-medium">{{ file.name }}</span>
          </div>
          <div v-if="probe" class="flex flex-wrap gap-4 rounded-md bg-muted/50 px-3 py-2 text-sm">
            <span>时长 <b>{{ formatDuration(probe.duration) }}</b></span>
            <span>大小 <b>{{ formatBytes(probe.size) }}</b></span>
            <span>格式 <b class="uppercase">{{ probe.ext }}</b></span>
          </div>
        </CardContent>
      </Card>

      <!-- 参数 -->
      <Card>
        <CardHeader>
          <CardTitle>切割参数</CardTitle>
        </CardHeader>
        <CardContent>
          <div class="grid gap-4 sm:grid-cols-2">
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">目标时长</Label>
              <Input v-model="targetDuration" placeholder="如 10:00" class="max-w-[120px]" />
              <span class="text-xs text-muted-foreground">MM:SS 或 HH:MM:SS</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">智能对齐</Label>
              <Switch v-model="smartAlign" />
              <span class="text-xs text-muted-foreground">把切点对齐到停顿处</span>
            </div>
            <div v-if="smartAlign" class="flex items-center gap-3">
              <Label class="w-24 shrink-0">偏移容差</Label>
              <Input v-model.number="tolerance" type="number" min="5" max="30" class="max-w-[100px]" />
              <span class="text-xs text-muted-foreground">秒（5–30）</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">命名格式</Label>
              <Input v-model="namingFormat" placeholder="书名 第 {} 集" class="max-w-[160px]" />
              <span class="text-xs text-muted-foreground">完整文件名，{} 为编号</span>
            </div>
            <div class="flex items-center gap-3">
              <Label class="w-24 shrink-0">起始编号</Label>
              <Input v-model="startNumber" class="max-w-[100px]" />
            </div>
          </div>
        </CardContent>
      </Card>

      <!-- 方案 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2">
            <Wand2 class="h-5 w-5" />切割方案
          </CardTitle>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button variant="outline" @click="buildPlan" :disabled="busyPlan || !file">
              <Loader2 v-if="busyPlan" class="h-4 w-4 animate-spin" />
              <Wand2 v-else class="h-4 w-4" />
              {{ smartAlign ? '生成智能方案' : '生成均分方案' }}
            </Button>
            <span v-if="plan" class="text-sm">
              共 <b>{{ plan.count }}</b> 段
              <WorkbenchStatus v-if="plan.aligned" variant="success" class="ml-2">已对齐</WorkbenchStatus>
              <WorkbenchStatus v-else variant="secondary" class="ml-2">均分</WorkbenchStatus>
            </span>
          </div>

          <!-- 智能方案进行中 -->
          <Alert v-if="planTask" variant="info" class="items-center">
            <Loader2 class="h-4 w-4 shrink-0 animate-spin" />
            <div class="flex-1">
              <div class="mb-1.5">{{ planTask.current || '检测停顿中…' }}</div>
              <Progress :value="planTask.progress" />
            </div>
            <Button variant="outline" size="sm" @click="cancel(planTask.id)">取消</Button>
          </Alert>

          <!-- 方案表 -->
          <div v-else-if="plan">
            <div v-if="plan.aligned" class="mb-2 flex gap-2 text-xs">
              <WorkbenchStatus variant="success">吸附 {{ plan.snapped }} 处</WorkbenchStatus>
              <WorkbenchStatus variant="warning" v-if="plan.fallbacks">回退 {{ plan.fallbacks }} 处</WorkbenchStatus>
            </div>
            <ScrollArea class="max-h-72 rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead class="w-16">集</TableHead>
                    <TableHead class="w-28 text-right">起始</TableHead>
                    <TableHead class="w-28 text-right">时长</TableHead>
                    <TableHead class="w-28 text-right">结束</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow v-for="s in plan.segments" :key="s.index">
                    <TableCell>第 {{ s.index + 1 }} 集</TableCell>
                    <TableCell class="text-right font-mono">{{ formatDuration(s.start) }}</TableCell>
                    <TableCell class="text-right font-mono">{{ formatDuration(s.duration) }}</TableCell>
                    <TableCell class="text-right font-mono">{{ formatDuration(s.start + s.duration) }}</TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </ScrollArea>
          </div>
        </CardContent>
      </Card>

      <!-- 切割 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2">
            <Scissors class="h-5 w-5" />开始切割
          </CardTitle>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button @click="doCut" :disabled="busyCut || !plan || !projectSet">
              <Loader2 v-if="busyCut" class="h-4 w-4 animate-spin" />
              <Scissors v-else class="h-4 w-4" />
              {{ busyCut ? '切割中…' : '开始切割' }}
            </Button>
            <Button variant="outline" @click="doZip" :disabled="busyZip || !cutResult || !projectSet">
              <Package class="h-4 w-4" />
              打包下载
            </Button>
            <Button variant="outline" @click="doExport" :disabled="busyExport || !cutResult || !projectSet">
              <FolderOutput class="h-4 w-4" />
              输出到音频源文件夹
            </Button>
          </div>

          <!-- 切割进行中 -->
          <Alert v-if="cutTask" variant="info" class="items-center">
            <Loader2 class="h-4 w-4 shrink-0 animate-spin" />
            <div class="flex-1">
              <div class="mb-1.5">{{ cutTask.current || '切割中…' }}</div>
              <Progress :value="cutTask.progress" />
            </div>
            <Button variant="outline" size="sm" @click="cancel(cutTask.id)">取消</Button>
          </Alert>

          <!-- 结果 -->
          <div v-if="cutResult">
            <p class="mb-2 text-sm text-muted-foreground">
              共 <b class="text-foreground">{{ cutResult.file_count }}</b> 个文件 · 输出目录 {{ cutResult.output_dir }}
            </p>
            <ScrollArea class="max-h-80 rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>文件名</TableHead>
                    <TableHead class="w-28 text-right">时长</TableHead>
                    <TableHead class="w-24 text-right">大小</TableHead>
                    <TableHead class="w-24"></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow v-for="f in cutResult.files" :key="f.path">
                    <TableCell class="max-w-[360px] truncate">{{ f.name }}</TableCell>
                    <TableCell class="text-right font-mono">{{ formatDuration(f.duration) }}</TableCell>
                    <TableCell class="text-right">{{ formatBytes(f.size) }}</TableCell>
                    <TableCell>
                      <div class="flex items-center justify-end gap-1">
                        <Button variant="ghost" size="sm" @click="download(f.path)">
                          <Download class="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </ScrollArea>
          </div>
        </CardContent>
      </Card>

      <Alert v-if="error" variant="destructive">
        <XCircle class="h-4 w-4 shrink-0" />{{ error }}
      </Alert>
    </div>
  </div>
</template>
