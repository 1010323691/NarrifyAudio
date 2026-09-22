<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useProjectStore } from '@/stores/project'
import { useToast } from '@/components/ui/toast'
import { cutAudio, detectSilences, exportAudio, planAudio, probeAudio, zipAudio } from '@/api/audio'
import { cancelDurableTask, getDurableTask, listDurableTasks } from '@/api/persistentTasks'
import type { DurableTask } from '@/api/persistentTasks'
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
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import Progress from '@/components/ui/Progress.vue'
import ScrollArea from '@/components/ui/ScrollArea.vue'
import WorkspaceGateAlert from '@/components/ui/WorkspaceGateAlert.vue'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import Table from '@/components/ui/Table.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableRow from '@/components/ui/TableRow.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableCell from '@/components/ui/TableCell.vue'
import DirPicker from '@/components/DirPicker.vue'
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
const project = useProjectStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()

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

// 刷新恢复：页面重载后本地 taskId 丢失，但后端持久化任务仍在跑。
// 按持久化 task_type 区分两种在途任务重新挂接。
function reattachTasks() {
  void listDurableTasks().then((tasks) => {
    const active = tasks.filter((t) => ['pending', 'queued', 'running', 'cancelling', 'retrying'].includes(t.status))
    const planning = active.find((t) => t.task_type === 'audio.silences')
    const cutting = active.find((t) => t.task_type === 'audio.cut')
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
  })
}

async function trackTask(id: string, kind: 'plan' | 'cut') {
  try {
    while (true) {
      const task = await getDurableTask(id)
      if (kind === 'plan') planTask.value = task
      else cutTask.value = task
      if (['succeeded', 'failed', 'cancelled', 'timeout'].includes(task.status)) return
      await new Promise((resolve) => window.setTimeout(resolve, 500))
    }
  } catch (e: any) {
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
  busyProbe.value = true
  error.value = ''
  try {
    probe.value = await probeAudio(file.value.path)
  } catch (e: any) {
    error.value = e?.message || '无法读取音频'
    probe.value = null
  } finally {
    busyProbe.value = false
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
  busyPlan.value = true
  error.value = ''
  cutResult.value = null
  try {
    if (smartAlign.value) {
      // Long-running: pause detection + pause-aligned plan (a backend task).
      const { task_id } = await detectSilences(file.value.path, {
        targetDuration: targetDuration.value,
        alignTolerance: tolerance.value,
      })
      planTaskId.value = task_id
      planTask.value = await getDurableTask(task_id)
      void trackTask(task_id, 'plan')
      // Completion is handled by the watcher on planTask.status.
    } else {
      const r = await planAudio(file.value.path, targetDuration.value)
      plan.value = { segments: r.segments, count: r.count, aligned: false, snapped: 0, fallbacks: 0 }
      rememberParams()
    }
  } catch (e: any) {
    error.value = e?.message || '生成方案失败'
    planTaskId.value = null
    busyPlan.value = false
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
  busyCut.value = true
  error.value = ''
  try {
    // Pass the pre-computed segments so the cut matches exactly what was previewed.
    const { task_id } = await cutAudio(file.value.path, {
      segments: plan.value.segments,
      namingFormat: namingFormat.value,
      startNumber: startNumber.value,
      })
      cutTaskId.value = task_id
      cutTask.value = await getDurableTask(task_id)
      void trackTask(task_id, 'cut')
    // Remember the cut parameters (fire-and-forget; the cut above already carries them).
    rememberParams()
    // Completion is handled by the watcher on cutTask.status.
  } catch (e: any) {
    error.value = e?.message || '启动切割失败'
    cutTaskId.value = null
    busyCut.value = false
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
      project.recordAudio(r)
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
    const r = await zipAudio({ base, files: cutFilesSpec() })
    // 浏览器：下载 zip（后端回 Content-Disposition: attachment，存到下载目录）。
    download(r.zip_path)
    toast({ title: '打包已开始下载', variant: 'success', description: `打包 ${r.file_count} 个文件` })
  } catch (e: any) {
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
    const r = await exportAudio(file.value.path, { files: cutFilesSpec() })
    // 文件已落到磁盘——这本身就是成功。
    toast({ title: '已输出到源文件夹', variant: 'success', description: `${r.file_count} 个文件 → ${r.dest_dir}` })
  } catch (e: any) {
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
  <div class="space-y-4">
    <div>
      <h1 class="text-2xl font-bold tracking-tight">音频分集</h1>
      <p class="text-muted-foreground mt-1">
        将合并后的有声书切分为多集，可按停顿对齐切点。
      </p>
    </div>

    <WorkspaceGateAlert />

    <!-- 选择文件 -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <AudioLines class="h-5 w-5" />选择音频
        </CardTitle>
      </CardHeader>
      <CardContent class="space-y-3">
        <DirPicker
          module="06_audio_merge"
          :extensions="['mp3', 'wav', 'm4a', 'aac', 'ogg', 'oga', 'opus', 'flac', 'webm']"
          :show-default="false"
          v-model="selectedName"
          label="待分集音频（06_audio_merge/）"
          empty-hint="06_audio_merge/ 下暂无音频——请先到「音频合并」生成有声书。"
          @scanned="onScanned"
        />
        <div v-if="file" class="flex flex-wrap items-center gap-3 rounded-md bg-muted/50 px-3 py-2 text-sm">
          <span class="font-medium">{{ file.name }}</span>
          <span class="text-xs text-muted-foreground truncate max-w-[320px]" :title="file.path">{{ file.path }}</span>
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
            <Badge v-if="plan.aligned" variant="success" class="ml-2">已对齐</Badge>
            <Badge v-else variant="secondary" class="ml-2">均分</Badge>
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
            <Badge variant="success">吸附 {{ plan.snapped }} 处</Badge>
            <Badge variant="warning" v-if="plan.fallbacks">回退 {{ plan.fallbacks }} 处</Badge>
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
          <Button @click="doCut" :disabled="busyCut || !plan || !workspaceSet">
            <Loader2 v-if="busyCut" class="h-4 w-4 animate-spin" />
            <Scissors v-else class="h-4 w-4" />
            {{ busyCut ? '切割中…' : '开始切割' }}
          </Button>
          <Button variant="outline" @click="doZip" :disabled="busyZip || !cutResult || !workspaceSet">
            <Package class="h-4 w-4" />
            打包下载
          </Button>
          <Button variant="outline" @click="doExport" :disabled="busyExport || !cutResult || !workspaceSet">
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
                  <TableCell class="max-w-[360px] truncate" :title="f.path">{{ f.name }}</TableCell>
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
</template>
