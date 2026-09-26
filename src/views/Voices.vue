<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { usePipelineStateStore } from '@/stores/pipelineState'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { listVoices, generateVoiceCandidates, mergeSpeakers, prepareFoundations, selectVoice, setGender, ttsStatus } from '@/api/tts'
import { downloadUrl } from '@/utils/fileops'
import type { MakeClonesResult, PrepareFoundationsResult, TTSStatus, VoiceItem } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Input from '@/components/ui/Input.vue'
import Select from '@/components/ui/Select.vue'
import Badge from '@/components/ui/Badge.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import Alert from '@/components/ui/Alert.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import WorkspaceEntryPicker from '@/components/WorkspaceEntryPicker.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'
import {
  Users,
  Sparkles,
  AudioWaveform,
  Loader2,
  X,
  XCircle,
  CheckCircle2,
  RefreshCw,
  ArrowRight,
  FolderOpen,
  Merge,
  Copy,
  Search,
  HelpCircle,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const pipeline = usePipelineStateStore()
const taskStore = useTaskStore()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()

const status = ref<TTSStatus | null>(null)
const hasScript = ref(false)
const speakers = ref<VoiceItem[]>([])

// Per-character optional description overrides (a single-char Phase-1 regenerate honours these).
const prompts = reactive<Record<string, string>>({})

const error = ref('')

// Phase 1 (语音推理基础, LLM only) task state.
const foundationBusy = ref(false)
const foundationTaskId = ref<string | null>(null)
const foundationResult = ref<PrepareFoundationsResult | null>(null)
const foundationTask = computed(() => taskStore.projectTasks.find((t) => t.id === foundationTaskId.value) ?? null)

// Phase 2 (克隆音频, TTS only) task state.
const cloneBusy = ref(false)
const cloneTaskId = ref<string | null>(null)
const cloneResult = ref<MakeClonesResult | null>(null)
const cloneTask = computed(() => taskStore.projectTasks.find((t) => t.id === cloneTaskId.value) ?? null)

// Phase 2 rows-per-batch cap (ONE long-lived worker process, tensor batches; VRAM scales
// with it). Seeded from config — shares tts.batch_concurrency with 音频合成.
const cloneConcurrency = ref(4)

// Per-character clone-candidate count for Phase 2: 'auto' (absolute log-scale ladder on each
// character's OWN line count — a 20k-line 旁白 can't demote the leads) or a fixed 2/4/6/8.
// Page-local (not persisted to config) — a per-run choice.
const candidateCount = ref('auto')

// 选择音色 overlay state: which character's candidates are shown, and which candidate the
// user staged (null = no explicit pick → the default first candidate stays active).
const pickerName = ref<string | null>(null)
const pickerChoice = ref<string | null>(null)
const pickerBusy = ref(false)
const pickerError = ref('')
// The overlay always reads the LATEST list (a running task's progress watcher re-loads it,
// so the rows track live results); the character vanishing from the list closes the overlay.
const pickerTarget = computed(
  () => (pickerName.value ? speakers.value.find((s) => s.name === pickerName.value) ?? null : null),
)
watch(pickerTarget, (t) => {
  if (!t && pickerName.value) closePicker()
})

// 合并角色 sub-window state: which row launched it, the search filter, the staged target,
// the confirm step, and the in-flight write. Every row's 合并 button shares this one state —
// only the source name differs.
const mergeSource = ref<string | null>(null)
const mergeQuery = ref('')
const mergeTarget = ref<string | null>(null)
const mergeConfirm = ref(false)
const mergeBusy = ref(false)
const mergeError = ref('')
// Like pickerTarget: always read the LATEST list (a refresh removes the merged-away row →
// the window closes itself).
const mergeSourceItem = computed(
  () => (mergeSource.value ? speakers.value.find((s) => s.name === mergeSource.value) ?? null : null),
)
watch(mergeSourceItem, (t) => {
  if (!t && mergeSource.value) closeMerge()
})
// Searchable target list: every character except the source itself (auto-filter).
const mergeOptions = computed(() => {
  const q = mergeQuery.value.trim().toLowerCase()
  return speakers.value.filter(
    (s) => s.name !== mergeSource.value && (!q || s.name.toLowerCase().includes(q)),
  )
})

// 性别徽章（人名旁 ♂/♀）：阶段 1 预填、点击纠正的小菜单。菜单用 fixed 定位（表格外层
// overflow-x-auto 会裁剪 absolute 下拉），坐标在打开时按按钮 rect 快照。
const genderMenu = ref<{ name: string; x: number; y: number } | null>(null)
const genderBusy = ref(false)
function openGenderMenu(v: VoiceItem, el: HTMLElement) {
  if (genderMenu.value?.name === v.name) {
    genderMenu.value = null
    return
  }
  const r = el.getBoundingClientRect()
  genderMenu.value = {
    name: v.name,
    x: Math.min(r.left, window.innerWidth - 104),
    y: Math.min(r.bottom + 4, window.innerHeight - 132),
  }
}
function closeGenderMenu() {
  genderMenu.value = null
}
// Direction A badge styling: muted 10% fill + 30% border + 600/300 text (dark mode aware).
function genderBadgeClass(g: VoiceItem['gender']): string {
  const base = 'inline-flex h-5 select-none items-center gap-1 rounded-full border px-1.5 text-[11px] leading-none transition-opacity'
  if (g === 'male') return `${base} cursor-pointer border-indigo-500/30 bg-indigo-500/10 text-indigo-600 hover:opacity-80 dark:text-indigo-300 disabled:cursor-not-allowed disabled:opacity-40`
  if (g === 'female') return `${base} cursor-pointer border-rose-500/30 bg-rose-500/10 text-rose-600 hover:opacity-80 dark:text-rose-300 disabled:cursor-not-allowed disabled:opacity-40`
  return `${base} cursor-pointer border-border bg-transparent text-muted-foreground opacity-70 hover:opacity-90 disabled:cursor-not-allowed`
}
async function applyGender(v: VoiceItem, g: 'male' | 'female' | '') {
  if (genderBusy.value) return
  genderBusy.value = true
  try {
    await setGender(v.name, g)
    v.gender = g
    closeGenderMenu()
    toast({
      title: g ? `性别已标记为「${g === 'male' ? '男' : '女'}」` : '已清除性别标记',
      variant: 'success',
      description: v.name,
    })
    loadVoices()
  } catch (e: any) {
    toast({ title: '性别标记失败', variant: 'destructive', description: e?.message || '保存失败' })
  } finally {
    genderBusy.value = false
  }
}

// Which characters the in-flight phase task targets: null = 全部（批量）; a list = the specific
// row(s) of a single-character run. Lets the per-row 生成中/制作中 overlay light up ONLY the rows
// that run actually touches (so a single-character regen doesn't mark every 未生成 row as in-flight).
const foundationTargets = ref<string[] | null>(null)
const cloneTargets = ref<string[] | null>(null)

// The parsed-JSON selection on THIS page. Local (not the shared store) so the whole-book
// "全部文件" scope ('__all__') never leaks into 音频合成, which is per-file.
// '' = most recent; a file name = that file; '__all__' = every file in 03_parsed_json/.
const ALL_SCRIPT = '__all__'
const scope = ref(pipeline.activeScript || '')
// Which parsed JSON(s) to read (mirrors the picker; '__all__' aggregates every file).
const script = computed(() => scope.value)

// A phase is "running" while any of its tasks is active (drives the per-row 生成中/制作中 overlay).
const ACTIVE: string[] = ['pending', 'running', 'paused']
const foundationRunning = computed(() => taskStore.projectTasks.some((t) => t.module === 'voices-foundation' && ACTIVE.includes(t.status)))
const cloneRunning = computed(() => taskStore.projectTasks.some((t) => t.module === 'voices-clone' && ACTIVE.includes(t.status)))

// Button gating: a phase is blocked while its own launch is in flight, while the OTHER phase
// is running (so the LLM and TTS never share the GPU), or with no workspace / script.
const foundationBlocked = computed(() => foundationBusy.value || cloneRunning.value || !hasScript.value || !projectSet.value)
const cloneBlocked = computed(() => cloneBusy.value || foundationRunning.value || !hasScript.value || !projectSet.value)

// Progress + readiness (denominator = non-alias characters).
const nonAlias = computed(() => speakers.value.filter((s) => !s.alias_of))
const foundationDone = computed(() => nonAlias.value.filter((s) => s.foundation_status === 'done').length)
const cloneDone = computed(() => nonAlias.value.filter((s) => s.clone_status === 'done').length)
const readyCount = computed(() => speakers.value.filter((s) => s.status === 'ready').length)

type BadgeVariant = 'default' | 'secondary' | 'destructive' | 'success' | 'warning' | 'outline'
interface PhaseBadge { label: string; variant: BadgeVariant; spin: boolean }

// A run lights up a row when it is a batch (targets === null) or explicitly names that character.
function inTargets(list: string[] | null, name: string) {
  return list === null || list.includes(name)
}

// Phase 1 (语音推理基础) badge: done/failed from the stored state; a row the running task
// targets (and that isn't done/failed yet) shows 生成中 (spinner) — only the rows that run touches.
function foundationBadge(v: VoiceItem): PhaseBadge {
  if (v.foundation_status === 'done') return { label: '已生成', variant: 'success', spin: false }
  if (v.foundation_status === 'failed') return { label: '失败', variant: 'destructive', spin: false }
  if (foundationRunning.value && inTargets(foundationTargets.value, v.name)) return { label: '生成中', variant: 'warning', spin: true }
  return { label: '未生成', variant: 'secondary', spin: false }
}

// Phase 2 (克隆音频) badge: same derivation against clone_status / cloneRunning / cloneTargets.
function cloneBadge(v: VoiceItem): PhaseBadge {
  if (v.clone_status === 'done') return { label: '已完成', variant: 'success', spin: false }
  if (v.clone_status === 'failed') return { label: '失败', variant: 'destructive', spin: false }
  if (cloneRunning.value && inTargets(cloneTargets.value, v.name)) return { label: '制作中', variant: 'warning', spin: true }
  return { label: '未制作', variant: 'secondary', spin: false }
}

async function loadVoices() {
  try {
    const r = await listVoices(script.value || undefined)
    hasScript.value = r.has_script
    speakers.value = r.speakers
  } catch {
    hasScript.value = false
    speakers.value = []
  }
}

// Local → store: a concrete file / most-recent keeps 音频合成 in step; the "all files"
// scope is Voices-local and must not be written to the shared selection.
watch(scope, (v) => {
  loadVoices()
  if (v !== ALL_SCRIPT) pipeline.activeScript = v
})
// Store → local: under keep-alive this page is cached, so a pick made on 音频合成 must
// refresh the (cached) character list. Guarded so an active "all files" view is kept.
watch(() => pipeline.activeScript, (v) => {
  if (scope.value !== ALL_SCRIPT && v !== scope.value) scope.value = v
})

// 刷新恢复：页面重载后本地 taskId 丢失，但后端任务仍在跑（store 的 refresh 已拉回全量任务）。
// 按 module 重新挂接在途的阶段 1 / 阶段 2 任务——恢复日志面板绑定、按钮门控与完成 watcher
//（行内「生成中/制作中」overlay 本就是 module 派生，刷新后已自行恢复）。
function reattachTasks() {
  const f = taskStore.activeTasks('voices-foundation')[0]
  if (f) {
    foundationTaskId.value = f.id
    foundationBusy.value = true
  }
  const c = taskStore.activeTasks('voices-clone')[0]
  if (c) {
    cloneTaskId.value = c.id
    cloneBusy.value = true
  }
}

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  cloneConcurrency.value = Math.min(64, Math.max(1, settings.config?.tts.batch_concurrency ?? 4))
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  await loadVoices()
  await taskStore.refresh()
  reattachTasks()
})

async function doFoundations(opts: {
  speakers?: string[]
  new_only?: boolean
  overrides?: Record<string, string>
}) {
  if (foundationBusy.value || cloneRunning.value) return
  foundationBusy.value = true
  error.value = ''
  foundationResult.value = null
  foundationTargets.value = opts.speakers ?? null
  try {
    const { task_id } = await prepareFoundations({ ...opts, script: script.value || undefined })
    foundationTaskId.value = task_id
    await taskStore.refresh()
    // Completion is handled by the watcher on foundationTask.status.
  } catch (e: any) {
    error.value = e?.message || '启动失败'
    foundationBusy.value = false
  }
}

async function doClones(opts: {
  speakers?: string[]
  new_only?: boolean
}) {
  if (cloneBusy.value || foundationRunning.value) return
  cloneBusy.value = true
  error.value = ''
  cloneResult.value = null
  cloneTargets.value = opts.speakers ?? null
  try {
    const { task_id } = await generateVoiceCandidates({
      ...opts,
      concurrency: cloneConcurrency.value,
      script: script.value || undefined,
      candidate_count: candidateCount.value === 'auto' ? null : Number(candidateCount.value),
    })
    cloneTaskId.value = task_id
    await taskStore.refresh()
    // Completion is handled by the watcher on cloneTask.status.
  } catch (e: any) {
    error.value = e?.message || '启动失败'
    cloneBusy.value = false
  }
}

// Coerce the rows-per-batch input (the Input component emits a string) to an integer in
// [1, 64] — the backend's clamp_concurrency is the final guard.
function onConcurrency(v: string | number) {
  const n = Math.trunc(Number(v))
  cloneConcurrency.value = Number.isFinite(n) ? Math.min(64, Math.max(1, n)) : 1
}

// Phase 1: re-call the LLM for this character's foundation (honours the per-row prompt override).
function regenFoundation(v: VoiceItem) {
  const prompt = (prompts[v.name] || '').trim()
  doFoundations({ speakers: [v.name], overrides: prompt ? { [v.name]: prompt } : {} })
}

function copyDescriptionToPrompt(v: VoiceItem) {
  prompts[v.name] = v.description || ''
}

// Phase 2: render this character's clone audio from its existing foundation (force a redo).
function remakeClone(v: VoiceItem) {
  doClones({ speakers: [v.name] })
}

function previewUrl(v: VoiceItem): string {
  return v.preview ? downloadUrl('04_voice_profiles', v.preview) : ''
}

function candidateUrl(c: { preview: string }): string {
  return c.preview ? downloadUrl('04_voice_profiles', c.preview) : ''
}

// The currently ACTIVE candidate id: the explicit pick, or the default first one.
function activeCandidateId(v: VoiceItem): string {
  return v.selected_audio_id ?? '1'
}

// 选择音色 is only meaningful once the character holds ≥2 distinct candidates (auto-mode
// cameos get 1, as do legacy single-take entries — those rows show 默认 and stay disabled).
function pickDisabled(v: VoiceItem): boolean {
  return cloneRunning.value || foundationRunning.value || (v.candidates?.length ?? 0) < 2
}

function pickLabel(v: VoiceItem): string {
  return v.selected_audio_id ? `已选 #${v.selected_audio_id}` : '默认'
}

function openPicker(v: VoiceItem) {
  pickerError.value = ''
  pickerChoice.value = v.selected_audio_id // null = the default first candidate
  pickerName.value = v.name
}

function closePicker() {
  pickerName.value = null
  pickerChoice.value = null
  pickerError.value = ''
}

async function confirmPick() {
  const v = pickerTarget.value
  if (!v || pickerBusy.value) return
  pickerBusy.value = true
  pickerError.value = ''
  try {
    const r = await selectVoice(v.name, pickerChoice.value)
    const what = r.selected_audio_id ? `候选 #${r.selected_audio_id}` : '默认（第 1 条）'
    toast({ title: '音色已更新', variant: 'success', description: `已为 ${r.speaker} 应用 ${what} 作为最终音色` })
    closePicker()
    loadVoices()
  } catch (e: any) {
    pickerError.value = e?.message || '保存失败'
  } finally {
    pickerBusy.value = false
  }
}

// 合并角色：把 source 角色的全部台词直接改写进 Parse 源数据（零 LLM 调用），删除其声音
// 配置。流程 = 选人子窗口（搜索筛选）→ 二次确认 → 同步写 → 刷新角色列表。
function openMerge(v: VoiceItem) {
  mergeError.value = ''
  mergeQuery.value = ''
  mergeTarget.value = null
  mergeConfirm.value = false
  mergeSource.value = v.name
}

function closeMerge() {
  mergeSource.value = null
  mergeQuery.value = ''
  mergeTarget.value = null
  mergeConfirm.value = false
  mergeError.value = ''
}

async function confirmMerge() {
  const src = mergeSource.value
  const tgt = mergeTarget.value
  if (!src || !tgt || mergeBusy.value) return
  mergeBusy.value = true
  mergeError.value = ''
  try {
    const r = await mergeSpeakers(src, tgt, script.value || undefined)
    toast({
      title: '角色已合并',
      variant: 'success',
      description: `已将 ${r.source} 的 ${r.replaced} 条台词并入 ${r.target}${r.files.length ? `（${r.files.join('、')}）` : ''}`,
    })
    closeMerge()
    loadVoices()
  } catch (e: any) {
    mergeError.value = e?.message || '合并失败'
  } finally {
    mergeBusy.value = false
  }
}

function cancelFoundation() {
  if (foundationTask.value) taskStore.control(foundationTask.value.id, 'cancel')
}
function cancelClone() {
  if (cloneTask.value) taskStore.control(cloneTask.value.id, 'cancel')
}

watch(
  () => foundationTask.value?.status,
  (st) => {
    const t = foundationTask.value
    if (!st || !t) return
    if (st === 'succeeded') {
      foundationResult.value = t.result as PrepareFoundationsResult
      foundationBusy.value = false
      toast({ title: '语音推理基础生成完成', variant: 'success', description: `已为 ${foundationResult.value?.count ?? 0} 个角色生成基础（${foundationResult.value?.aliases ?? 0} 个别名）` })
      // Keep foundationTaskId set so the log panel stays visible with the final logs; the next
      // run simply overwrites it.
      loadVoices()
    } else if (st === 'failed') {
      error.value = t.error || '语音推理基础生成失败'
      foundationBusy.value = false
      toast({ title: '语音推理基础生成失败', variant: 'destructive', description: error.value })
      loadVoices() // reflect characters that completed before the failure
    } else if (st === 'cancelled') {
      foundationBusy.value = false
      loadVoices() // reflect characters that completed before the cancel
    }
  },
)

// While the foundation task is running, refresh the character list on each progress event so
// each character's 语音推理基础 badge (and any preview) updates the moment it completes — the
// backend persists per-character as each LLM call finishes, so this poll picks it up live.
watch(
  () => foundationTask.value?.progress,
  (p) => {
    const t = foundationTask.value
    if (p == null || !t) return
    if (ACTIVE.includes(t.status)) loadVoices()
  },
)

watch(
  () => cloneTask.value?.status,
  (st) => {
    const t = cloneTask.value
    if (!st || !t) return
    if (st === 'succeeded') {
      cloneResult.value = t.result as MakeClonesResult
      cloneBusy.value = false
      toast({ title: '克隆音频制作完成', variant: 'success', description: `成功 ${cloneResult.value?.ok ?? 0} / 失败 ${cloneResult.value?.failed ?? 0} / 共 ${cloneResult.value?.count ?? 0} 个角色` })
      // Keep cloneTaskId set so the log panel stays visible with the final logs; the next run
      // simply overwrites it.
      loadVoices()
    } else if (st === 'failed') {
      error.value = t.error || '克隆音频制作失败'
      cloneBusy.value = false
      toast({ title: '克隆音频制作失败', variant: 'destructive', description: error.value })
      loadVoices() // reflect characters that completed before the failure
    } else if (st === 'cancelled') {
      cloneBusy.value = false
      loadVoices() // reflect characters that completed before the cancel
    }
  },
)

// While the clone task is running, refresh the character list on each progress event so each
// character's 克隆音频 badge (and its 试听 preview) appears the moment that render completes.
watch(
  () => cloneTask.value?.progress,
  (p) => {
    const t = cloneTask.value
    if (p == null || !t) return
    if (ACTIVE.includes(t.status)) loadVoices()
  },
)
</script>

<template>
  <!-- -mx-16：角色表新增「选择音色」按钮后 1152px 列宽不够——本页整体向两侧各借 64px（≈一个按钮宽），
       只借 MainLayout max-w-6xl 居中留出的空白，不改共享布局，其余页面不受影响。 -->
  <div class="space-y-6">
    <header class="page-header mb-5">
      <div>
        <p class="eyebrow">Pipeline · Voices</p>
        <h1 class="page-title flex items-center gap-3">
          角色配音
          <StatusPill
            :label="(status?.ready ?? status?.implemented) ? '可用' : '引擎未就绪'"
            :tone="(status?.ready ?? status?.implemented) ? 'positive' : 'neutral'"
            :aria-label="(status?.ready ?? status?.implemented) ? '引擎可用' : '引擎未就绪'"
          />
        </h1>
        <p class="page-description">为角色生成候选音色，并选择每个角色使用的最终音色。</p>
      </div>
    </header>

    <ProjectGateAlert />

    <Alert v-if="status && !(status.ready ?? status.implemented)" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ status.message }}
    </Alert>

    <template v-else>
      <Alert v-if="!hasScript" variant="default">
        <Users class="h-4 w-4 shrink-0" />
        尚未检测到角色，请先在「文本解析」生成解析结果。
      </Alert>

      <!-- 阶段 1 · 生成语音推理基础（LLM only） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><Sparkles class="h-5 w-5" />阶段 1 · 生成语音推理基础</CardTitle>
          <CardDescription>
            为角色生成声音描述和种子文案。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="foundationBlocked" @click="doFoundations({})">
              <Loader2 v-if="foundationBusy" class="h-4 w-4 animate-spin" />
              <Sparkles v-else class="h-4 w-4" />
              {{ foundationBusy ? '生成中…' : '批量生成所有角色' }}
            </Button>
            <Button variant="outline" :disabled="foundationBlocked" @click="doFoundations({ new_only: true })">
              <Users class="h-4 w-4" />仅新增角色
            </Button>
            <Button variant="outline" size="sm" @click="loadVoices">
              <RefreshCw class="h-4 w-4" />刷新
            </Button>
            <span class="ml-auto text-xs text-muted-foreground">语音推理基础：{{ foundationDone }} / {{ nonAlias.length }}</span>
          </div>

          <LiveLogPanel :task="foundationTask" :max-height-class="'h-72'">
            <template #actions>
              <Button v-if="foundationTask && ACTIVE.includes(foundationTask.status)" variant="outline" size="sm" @click="cancelFoundation">
                <XCircle class="h-3.5 w-3.5" />取消
              </Button>
            </template>
          </LiveLogPanel>

          <div
            v-if="foundationResult"
            class="flex items-center gap-2 rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-400"
          >
            <CheckCircle2 class="h-4 w-4 shrink-0" />
            完成：为 {{ foundationResult.count }} 个角色生成语音推理基础，识别 {{ foundationResult.aliases }} 个别名（未启动 TTS）。
          </div>
        </CardContent>
      </Card>

      <!-- 阶段 2 · 制作克隆音频（TTS only） -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><AudioWaveform class="h-5 w-5" />阶段 2 · 制作克隆音频</CardTitle>
          <CardDescription>
            为角色生成候选音色。请先关闭 LLM，再开始制作。
          </CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div class="flex flex-wrap items-center gap-3">
            <Button :disabled="cloneBlocked" @click="doClones({ new_only: true })">
              <Loader2 v-if="cloneBusy" class="h-4 w-4 animate-spin" />
              <AudioWaveform v-else class="h-4 w-4" />
              {{ cloneBusy ? '制作中…' : '批量制作克隆音频' }}
            </Button>
            <label class="flex items-center gap-2 text-sm text-muted-foreground">
              批内行数（上限）
              <Input
                :modelValue="cloneConcurrency"
                type="number"
                min="1"
                max="64"
                class="h-8 w-20"
                :disabled="cloneBlocked"
                @update:modelValue="onConcurrency"
              />
            </label>
            <label class="flex items-center gap-2 text-sm text-muted-foreground">
              备选音频数
              <Select
                :modelValue="candidateCount"
                class="h-8 w-28"
                :disabled="cloneBlocked"
                @update:modelValue="candidateCount = String($event)"
              >
                <option value="auto">自动</option>
                <option value="2">2</option>
                <option value="4">4</option>
                <option value="6">6</option>
                <option value="8">8</option>
              </Select>
            </label>
            <span class="ml-auto text-xs text-muted-foreground">克隆音频：{{ cloneDone }} / {{ nonAlias.length }}</span>
          </div>

          <LiveLogPanel :task="cloneTask" :max-height-class="'h-72'">
            <template #actions>
              <Button v-if="cloneTask && ACTIVE.includes(cloneTask.status)" variant="outline" size="sm" @click="cancelClone">
                <XCircle class="h-3.5 w-3.5" />取消
              </Button>
            </template>
          </LiveLogPanel>

          <div
            v-if="cloneResult"
            class="flex items-center gap-2 rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-400"
          >
            <CheckCircle2 class="h-4 w-4 shrink-0" />
            完成：克隆音频成功 {{ cloneResult.ok }} / 失败 {{ cloneResult.failed }} / 共 {{ cloneResult.count }} 个角色。
          </div>
        </CardContent>
      </Card>

      <!-- 工作区目录 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2"><FolderOpen class="h-5 w-5" />工作区目录</CardTitle>
          <CardDescription>选择要使用的解析脚本。</CardDescription>
        </CardHeader>
        <CardContent>
          <WorkspaceEntryPicker
            module="03_parsed_json"
            :extensions="['json']"
            exclude-suffix="_checked.json"
            v-model="scope"
            :show-all="true"
            :all-value="ALL_SCRIPT"
            label="解析结果"
          />
        </CardContent>
      </Card>

      <!-- 角色 -->
      <Card>
        <CardHeader>
          <CardTitle class="flex items-center gap-2">
            <Users class="h-5 w-5" />角色（{{ speakers.length }}）
          </CardTitle>
          <CardDescription v-if="speakers.length">已就绪 {{ readyCount }} / {{ speakers.length }} · 基础 {{ foundationDone }} / 克隆 {{ cloneDone }} / {{ nonAlias.length }}</CardDescription>
        </CardHeader>
        <CardContent class="space-y-4">
          <div v-if="speakers.length" class="overflow-x-auto">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b text-left text-xs text-muted-foreground">
                  <th class="pb-2 font-medium">角色</th>
                  <th class="pb-2 font-medium">台词数</th>
                  <th class="pb-2 font-medium">语音推理基础</th>
                  <th class="pb-2 font-medium">克隆音频</th>
                  <th class="pb-2 font-medium">声音描述 / 提示词</th>
                  <th class="pb-2 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="v in speakers" :key="v.name" class="border-b align-top last:border-0">
                  <td class="py-2 pr-3 font-medium">
                    <div class="whitespace-nowrap">
                      {{ v.name }}
                      <span v-if="v.alias_of" class="ml-1 text-xs text-muted-foreground">→ {{ v.alias_of }}</span>
                      <!-- 性别徽章：阶段 1 预填、点击纠正（alias 行借用目标音色，不单独标记） -->
                      <button
                        v-else
                        type="button"
                        class="ml-1.5 align-middle"
                        :class="genderBadgeClass(v.gender)"
                        :title="v.gender ? '点击修改性别标记' : '标记性别（阶段 1 自动推断，可点击纠正）'"
                        :disabled="genderBusy || foundationRunning || cloneRunning"
                        @click.stop="openGenderMenu(v, $event.currentTarget as HTMLElement)"
                      >
                        <!-- ♂/♀ = Unicode 性别符号（lucide 0.468 无 Mars/Venus 图标） -->
                        <span v-if="v.gender === 'male'" class="text-xs leading-none">♂</span>
                        <span v-else-if="v.gender === 'female'" class="text-xs leading-none">♀</span>
                        <HelpCircle v-else class="h-3 w-3" />
                        {{ v.gender === 'male' ? '男' : v.gender === 'female' ? '女' : '未定' }}
                      </button>
                    </div>
                    <div class="mt-1 flex items-center">
                      <Button
                        variant="outline"
                        size="sm"
                        class="h-6 w-6 p-0"
                        title="将该角色合并到其他角色（直接修改解析源数据）"
                        :disabled="foundationRunning || cloneRunning || speakers.length < 2"
                        @click="openMerge(v)"
                      >
                        <Merge class="h-3 w-3" />
                      </Button>
                    </div>
                  </td>
                  <td class="py-2 pr-3 text-muted-foreground">{{ v.line_count }}</td>
                  <td class="py-2 pr-3">
                    <Badge :variant="foundationBadge(v).variant">
                      <Loader2 v-if="foundationBadge(v).spin" class="mr-1 h-3 w-3 animate-spin" />
                      {{ foundationBadge(v).label }}
                    </Badge>
                  </td>
                  <td class="py-2 pr-3">
                    <Badge :variant="cloneBadge(v).variant">
                      <Loader2 v-if="cloneBadge(v).spin" class="mr-1 h-3 w-3 animate-spin" />
                      {{ cloneBadge(v).label }}
                    </Badge>
                  </td>
                  <td class="py-2 pr-3">
                    <!-- 固定列宽：描述/提示词列不再被操作列挤窄（表格整体可横向滚动兜底） -->
                    <div class="w-60 min-w-60">
                      <div class="truncate text-xs text-muted-foreground" :title="v.description">
                        {{ v.description || '—' }}
                      </div>
                      <div class="mt-1.5 flex items-center gap-1">
                        <Input
                          v-model="prompts[v.name]"
                          class="h-8 min-w-0 flex-1 text-xs"
                          placeholder="可选：自定义声音描述（阶段 1 重新生成时生效）"
                          :disabled="foundationBusy"
                        />
                        <Button
                          variant="outline"
                          size="sm"
                          class="h-8 w-8 shrink-0 p-0"
                          :title="`将 ${v.name} 的声音描述复制到提示词`"
                          :aria-label="`将 ${v.name} 的声音描述复制到提示词`"
                          :disabled="foundationBusy || !v.description"
                          @click="copyDescriptionToPrompt(v)"
                        >
                          <Copy class="h-3 w-3" aria-hidden="true" />
                        </Button>
                      </div>
                    </div>
                  </td>
                  <td class="py-2">
                    <div class="flex items-center justify-end gap-2">
                      <MiniAudioPlayer v-if="v.preview" :src="previewUrl(v)" />
                      <span class="w-14 shrink-0 text-right text-xs text-muted-foreground" :title="pickLabel(v)">
                        {{ pickLabel(v) }}
                      </span>
                      <Button variant="outline" size="sm" :disabled="pickDisabled(v)" @click="openPicker(v)">
                        选择音色
                      </Button>
                      <Button variant="outline" size="sm" :disabled="foundationBlocked" @click="regenFoundation(v)">
                        重新生成
                      </Button>
                      <Button variant="outline" size="sm" :disabled="cloneBlocked || v.foundation_status !== 'done'" @click="remakeClone(v)">
                        重新制作
                      </Button>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-else class="text-sm text-muted-foreground">（暂无角色）</p>
        </CardContent>
      </Card>

      <div v-if="hasScript && readyCount >= speakers.length && speakers.length > 0" class="flex justify-end">
        <Button size="sm" @click="router.push('/batch')">
          前往音频合成<ArrowRight class="h-4 w-4" />
        </Button>
      </div>
    </template>

    <Alert v-if="error" variant="destructive">
      <template #icon><XCircle class="h-4 w-4 shrink-0" /></template>
      {{ error }}
    </Alert>

    <!-- 选择音色 overlay：试听该角色全部候选克隆音频，单选一个为最终音色（单选后行内试听随之切换） -->
    <div
      v-if="pickerTarget"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="closePicker"
    >
      <div class="max-h-[80vh] w-full max-w-lg space-y-4 overflow-y-auto rounded-lg border bg-background p-5 shadow-lg">
        <div class="flex items-start justify-between gap-3">
          <div>
            <h2 class="text-lg font-semibold">选择音色 · {{ pickerTarget.name }}</h2>
            <p class="mt-1 text-xs text-muted-foreground">
              试听候选音频并选择最终音色；未选择时使用第 1 条。
            </p>
          </div>
          <Button variant="ghost" size="icon" :disabled="pickerBusy" @click="closePicker">
            <X class="h-4 w-4" />
          </Button>
        </div>

        <div class="space-y-1.5">
          <label
            v-for="c in pickerTarget.candidates"
            :key="c.id"
            class="flex cursor-pointer items-center gap-3 rounded px-2 py-1.5 hover:bg-accent/50"
            :class="{ 'bg-accent/60': pickerChoice === c.id }"
          >
            <input
              type="radio"
              class="h-4 w-4 shrink-0 accent-primary"
              name="voice-candidate"
              :checked="pickerChoice === c.id"
              :disabled="pickerBusy"
              @change="pickerChoice = c.id"
            />
            <span class="min-w-0 flex-1 truncate text-sm">
              候选 #{{ c.id }}
              <span v-if="c.id === '1'" class="ml-1 text-xs text-muted-foreground">（默认）</span>
            </span>
            <Badge v-if="c.id === activeCandidateId(pickerTarget)" variant="success" class="shrink-0">当前</Badge>
            <MiniAudioPlayer v-if="c.preview" :src="candidateUrl(c)" class="shrink-0" />
          </label>
          <p v-if="!pickerTarget.candidates.length" class="px-2 py-1.5 text-sm text-muted-foreground">
            该角色暂无候选音频——请先在阶段 2 制作克隆音频。
          </p>
        </div>

        <Alert v-if="pickerError" variant="destructive">{{ pickerError }}</Alert>

        <div class="flex items-center justify-end gap-2">
          <Button
            v-if="pickerTarget.selected_audio_id"
            variant="outline"
            size="sm"
            :disabled="pickerBusy"
            @click="pickerChoice = null"
          >
            恢复默认（第 1 条）
          </Button>
          <Button variant="outline" size="sm" :disabled="pickerBusy" @click="closePicker">取消</Button>
          <Button :disabled="pickerBusy || !pickerTarget.candidates.length" @click="confirmPick">
            <Loader2 v-if="pickerBusy" class="h-4 w-4 animate-spin" />
            确认
          </Button>
        </div>
      </div>
    </div>

    <!-- 性别标记菜单：fixed 定位（表格外层 overflow-x-auto 会裁剪 absolute 下拉），点外部即关 -->
    <template v-if="genderMenu">
      <div class="fixed inset-0 z-40" @click="closeGenderMenu"></div>
      <div
        class="fixed z-50 w-24 rounded-md border bg-background p-1 shadow-md"
        :style="{ left: genderMenu!.x + 'px', top: genderMenu!.y + 'px' }"
      >
        <button
          type="button"
          class="flex w-full items-center gap-1.5 rounded px-2 py-1 text-xs hover:bg-accent/60"
          :class="{ 'bg-accent/60': speakers.find((s) => s.name === genderMenu!.name)?.gender === 'male' }"
          :disabled="genderBusy"
          @click="applyGender(speakers.find((s) => s.name === genderMenu!.name)!, 'male')"
        >
          <span class="text-xs leading-none text-indigo-600 dark:text-indigo-300">♂</span>男
        </button>
        <button
          type="button"
          class="flex w-full items-center gap-1.5 rounded px-2 py-1 text-xs hover:bg-accent/60"
          :class="{ 'bg-accent/60': speakers.find((s) => s.name === genderMenu!.name)?.gender === 'female' }"
          :disabled="genderBusy"
          @click="applyGender(speakers.find((s) => s.name === genderMenu!.name)!, 'female')"
        >
          <span class="text-xs leading-none text-rose-600 dark:text-rose-300">♀</span>女
        </button>
        <button
          type="button"
          class="flex w-full items-center gap-1.5 rounded px-2 py-1 text-xs text-muted-foreground hover:bg-accent/60"
          :disabled="genderBusy"
          @click="applyGender(speakers.find((s) => s.name === genderMenu!.name)!, '')"
        >
          <HelpCircle class="h-3 w-3" />清除标记
        </button>
      </div>
    </template>

    <!-- 合并角色子窗口：把源角色的全部台词并入所选角色（直接改写解析源数据，零 LLM 调用） -->
    <div
      v-if="mergeSourceItem"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="closeMerge"
    >
      <div class="max-h-[80vh] w-full max-w-lg space-y-4 overflow-y-auto rounded-lg border bg-background p-5 shadow-lg">
        <div class="flex items-start justify-between gap-3">
          <div>
            <h2 class="text-lg font-semibold">合并角色 · {{ mergeSourceItem.name }}</h2>
            <p class="mt-1 text-xs text-muted-foreground">
              将该角色的台词并入所选角色，并移除原角色配置。
            </p>
          </div>
          <Button variant="ghost" size="icon" :disabled="mergeBusy" @click="closeMerge">
            <X class="h-4 w-4" />
          </Button>
        </div>

        <div class="relative">
          <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            v-model="mergeQuery"
            class="h-9 pl-8 text-sm"
            placeholder="搜索目标角色…"
            :disabled="mergeBusy"
          />
        </div>

        <div class="max-h-80 space-y-1.5 overflow-y-auto">
          <label
            v-for="s in mergeOptions"
            :key="s.name"
            class="flex cursor-pointer items-center gap-3 rounded px-2 py-1.5 hover:bg-accent/50"
            :class="{ 'bg-accent/60': mergeTarget === s.name }"
          >
            <input
              type="radio"
              class="h-4 w-4 shrink-0 accent-primary"
              name="merge-target"
              :checked="mergeTarget === s.name"
              :disabled="mergeBusy"
              @change="mergeTarget = s.name"
            />
            <span class="min-w-0 flex-1 truncate text-sm">
              {{ s.name }}
              <span v-if="s.alias_of" class="ml-1 text-xs text-muted-foreground">→ {{ s.alias_of }}</span>
            </span>
            <span class="shrink-0 text-xs text-muted-foreground">{{ s.line_count }} 条</span>
          </label>
          <p v-if="!mergeOptions.length" class="px-2 py-1.5 text-sm text-muted-foreground">
            没有匹配的目标角色。
          </p>
        </div>

        <Alert v-if="mergeError" variant="destructive">{{ mergeError }}</Alert>

        <div class="flex items-center justify-end gap-2">
          <Button variant="outline" size="sm" :disabled="mergeBusy" @click="closeMerge">取消</Button>
          <Button :disabled="mergeBusy || !mergeTarget" @click="mergeConfirm = true">
            下一步：确认合并
          </Button>
        </div>
      </div>
    </div>

    <!-- 合并角色 · 二次确认（盖在子窗口之上，保留目标选择上下文） -->
    <div
      v-if="mergeConfirm && mergeTarget"
      class="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4"
      @click.self="mergeConfirm = false"
    >
      <div class="w-full max-w-md space-y-4 rounded-lg border bg-background p-5 shadow-lg">
        <h2 class="text-lg font-semibold">确认合并？</h2>
        <p class="flex items-center gap-2 text-sm">
          <span class="font-medium">{{ mergeSourceItem?.name }}</span>
          <ArrowRight class="h-3.5 w-3.5" />
          <span class="font-medium">{{ mergeTarget }}</span>
        </p>
        <ul class="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
          <li>替换 {{ mergeSourceItem?.name }} 的 {{ mergeSourceItem?.line_count ?? 0 }} 条台词（当前范围：
            {{ scope === ALL_SCRIPT ? '全部文件' : (scope || '最近文件') }}）。</li>
          <li>删除 {{ mergeSourceItem?.name }} 的声音配置；指向它的别名将改指向目标角色。</li>
          <li>其候选音频文件保留在磁盘上，不会被删除。</li>
        </ul>
        <Alert v-if="mergeError" variant="destructive">{{ mergeError }}</Alert>
        <div class="flex items-center justify-end gap-2">
          <Button variant="outline" size="sm" :disabled="mergeBusy" @click="mergeConfirm = false">取消</Button>
          <Button :disabled="mergeBusy" @click="confirmMerge">
            <Loader2 v-if="mergeBusy" class="h-4 w-4 animate-spin" />
            确认合并
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>
