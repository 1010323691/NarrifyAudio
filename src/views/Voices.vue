<script setup lang="ts">
import Pager from '@/views/textformat/Pager.vue'
import { useListPage } from '@/composables/useListPage'
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import VoicesWorkbench from './voices/VoicesWorkbench.vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { latestEntryTasks } from '@/composables/useLabelDerivedTasks'
import { useWorkbenchTaskBatch } from '@/composables/useWorkbenchTaskBatch'
import { useWorkbenchScope, withinScope } from '@/composables/useWorkbenchScope'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { listVoices, generateVoiceCandidates, mergeSpeakers, prepareFoundations, selectVoice, setGender, ttsStatus } from '@/api/tts'
import { previewUrl as resourcePreviewUrl } from '@/utils/fileops'
import type { MakeClonesResult, PrepareFoundationsResult, TTSStatus, VoiceItem } from '@/types'

import WorkbenchActionBar from '@/components/WorkbenchActionBar.vue'
import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Input from '@/components/ui/Input.vue'
import WorkbenchStatus from '@/components/ui/WorkbenchStatus.vue'
import Alert from '@/components/ui/Alert.vue'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
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
  ArrowRight,
  Search,
  HelpCircle,
} from 'lucide-vue-next'

const router = useRouter()
const settings = useSettingsStore()
const auth = useAuthStore()
const project = useProjectStore()
const taskStore = useTaskStore()
const captureScope = useWorkbenchScope()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()

const status = ref<TTSStatus | null>(null)
const hasScript = ref(false)
const speakers = ref<VoiceItem[]>([])
const voicesLoading = ref(false)
const voicesLoadError = ref('')
let voicesRequest = 0

// Per-character optional description overrides (a single-char Phase-1 regenerate honours these).
const prompts = reactive<Record<string, string>>({})

const error = ref('')

// Phase 1 (语音推理基础, LLM only) task state.
const foundationBusy = ref(false)
const foundationSubmitting = ref(false)
const foundationTaskId = ref<string | null>(null)
const foundationResult = ref<PrepareFoundationsResult | null>(null)
const foundationBatch = useWorkbenchTaskBatch(foundationTaskId)
const foundationTask = foundationBatch.task

// Phase 2 (克隆音频, TTS only) task state.
const cloneBusy = ref(false)
const cloneSubmitting = ref(false)
const cloneTaskId = ref<string | null>(null)
const cloneResult = ref<MakeClonesResult | null>(null)
const cloneBatch = useWorkbenchTaskBatch(cloneTaskId)
const cloneTask = cloneBatch.task

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
const mergePage = ref(1)
const mergeTotal = ref(0)
watch(mergeQuery, () => { mergePage.value = 1 })
const mergeTarget = ref<string | null>(null)
const mergeConfirm = ref(false)
const mergeBusy = ref(false)
const mergeError = ref('')
// Like pickerTarget: always read the LATEST list (a refresh removes the merged-away row →
// the window closes itself).
const mergeSourceItem = computed(
  () => (mergeSource.value ? speakers.value.find((s) => s.name === mergeSource.value) ?? null : null),
)
const pickerPanel = ref<HTMLElement | null>(null)
const mergePanel = ref<HTMLElement | null>(null)
const mergeConfirmPanel = ref<HTMLElement | null>(null)
const activeOverlay = computed(() => mergeConfirm.value && mergeTarget.value ? 'confirm'
  : mergeSourceItem.value ? 'merge' : pickerTarget.value ? 'picker' : null)
let overlayReturnFocus: HTMLElement | null = null
let confirmReturnFocus: HTMLElement | null = null

function overlayPanel() {
  return activeOverlay.value === 'confirm' ? mergeConfirmPanel.value
    : activeOverlay.value === 'merge' ? mergePanel.value : pickerPanel.value
}
function overlayControls(panel: HTMLElement) {
  return [...panel.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),textarea:not([disabled]),a[href]')]
    .filter(el => el.getClientRects().length > 0)
}
watch(activeOverlay, async (active, previous) => {
  await nextTick()
  if (active !== activeOverlay.value) return
  const panel = overlayPanel()
  if (active && panel) {
    if (previous === 'confirm' && active === 'merge' && confirmReturnFocus?.isConnected) confirmReturnFocus.focus()
    else (panel.querySelector<HTMLElement>('[data-initial-focus]:not([disabled])') ?? overlayControls(panel)[0])?.focus()
  } else if (overlayReturnFocus?.isConnected) overlayReturnFocus.focus()
})
function openMergeConfirm() {
  confirmReturnFocus = document.activeElement as HTMLElement
  mergeConfirm.value = true
}
function overlayKeydown(event: KeyboardEvent) {
  const active = activeOverlay.value
  const panel = overlayPanel()
  if (!active || !panel) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    if (active === 'picker' && !pickerBusy.value) closePicker()
    else if (active === 'confirm' && !mergeBusy.value) mergeConfirm.value = false
    else if (active === 'merge' && !mergeBusy.value) closeMerge()
    return
  }
  if (event.key !== 'Tab') return
  const controls = overlayControls(panel)
  if (!controls.length) return // in-flight: every control is disabled — nothing to trap, so
  // let the browser's default Tab run instead of swallowing the key with no target.
  const first = controls[0], last = controls[controls.length - 1]
  if (!panel.contains(document.activeElement) || (event.shiftKey && document.activeElement === first)) {
    event.preventDefault()
    ;(event.shiftKey ? last : first)?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}
watch(mergeSourceItem, (t) => {
  if (!t && mergeSource.value) closeMerge()
})
watch([mergeSource, mergeQuery, mergePage], async () => {
  if (!mergeSource.value) return
  const isCurrent = captureScope()
  const source = mergeSource.value, q = mergeQuery.value, page = mergePage.value
  try {
    const result = await withinScope(listVoices(script, { page, page_size: 10, q, filter: 'all' }, undefined, false, true), isCurrent)
    if (mergeSource.value === source && mergeQuery.value === q && mergePage.value === page) { mergeTargetItems.value = result.speakers; mergeTotal.value = result.pagination?.total ?? 0 }
  } catch { /* The source row stays available while retrying a search. */ }
})
// Searchable target list: every character except the source itself (auto-filter).
const mergeTargetItems = ref<VoiceItem[]>([])
const mergeOptions = computed(() => {
  const q = mergeQuery.value.trim().toLowerCase()
  return mergeTargetItems.value.filter(
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

// Always cover all parsed chapters without changing downstream file selection.
const script = '__all__'

// A phase is "running" while any of its tasks is active (drives the per-row 生成中/制作中 overlay).
const ACTIVE: string[] = ['pending', 'queued', 'retrying', 'running', 'paused', 'cancelling']
const foundationRunning = computed(() => taskStore.projectTasks.some((t) => t.module === 'voices-foundation' && ACTIVE.includes(t.status)))
const cloneRunning = computed(() => taskStore.projectTasks.some((t) => t.module === 'voices-clone' && ACTIVE.includes(t.status)))

// Button gating: a phase is blocked while its own launch is in flight, while the OTHER phase
// is running (so the LLM and TTS never share the GPU), or with no workspace / script.
const foundationBlocked = computed(() => foundationBusy.value || cloneRunning.value || !hasScript.value || !projectSet.value)
const cloneBlocked = computed(() => cloneBusy.value || foundationRunning.value || !hasScript.value || !projectSet.value)

// Progress + readiness (denominator = non-alias characters).
const nonAlias = computed(() => speakers.value.filter((s) => !s.alias_of))
const foundationDone = computed(() => listPagination.value?.counts.foundation ?? nonAlias.value.filter((s) => s.foundation_status === 'done').length)
const cloneDone = computed(() => listPagination.value?.counts.clone ?? nonAlias.value.filter((s) => s.clone_status === 'done').length)
const readyCount = computed(() => listPagination.value?.counts.ready ?? speakers.value.filter((s) => s.status === 'ready').length)

type BadgeVariant = 'default' | 'secondary' | 'destructive' | 'success' | 'warning' | 'outline'
interface PhaseBadge { label: string; variant: BadgeVariant; spin: boolean }

// A run lights up a row when it is a batch (targets === null) or explicitly names that character.
function inTargets(list: string[] | null, name: string) {
  return list === null || list.includes(name)
}

// Use each character's task, including retained batch completions. A stored
// foundation is still usable during regeneration, but its old status must not
// hide the new task or keep finished rows spinning until the entire batch ends.
const foundationRows = computed(() => latestEntryTasks(
  [...taskStore.projectTasks, ...foundationBatch.rows.value], 'voices-foundation',
))
const cloneRows = computed(() => latestEntryTasks(
  [...taskStore.projectTasks, ...cloneBatch.rows.value], 'voices-clone',
))
function foundationBadge(v: VoiceItem): PhaseBadge {
  const task = foundationRows.value.get(v.name)
  if (task && ACTIVE.includes(task.status)) {
    if (task.status === 'paused') return { label: '已暂停', variant: 'warning', spin: false }
    if (task.status === 'cancelling') return { label: '取消中', variant: 'warning', spin: true }
    if (task.status === 'running') return { label: '处理中', variant: 'warning', spin: true }
    return { label: '排队中', variant: 'warning', spin: false }
  }
  if (foundationSubmitting.value && inTargets(foundationTargets.value, v.name)) {
    return { label: '处理中', variant: 'warning', spin: true }
  }
  if (task?.status === 'failed' || task?.status === 'timeout') return { label: '失败', variant: 'destructive', spin: false }
  if (task?.status === 'succeeded') {
    const completion = task.result?.results?.find((r: { speaker: string; ok: boolean }) => r.speaker === v.name)
    if (completion?.ok === false) return { label: '失败', variant: 'destructive', spin: false }
    if (completion?.ok === true) return { label: '已完成', variant: 'success', spin: false }
  }
  if (v.foundation_status === 'done') return { label: '已完成', variant: 'success', spin: false }
  if (v.foundation_status === 'failed') return { label: '失败', variant: 'destructive', spin: false }
  return { label: '未生成', variant: 'secondary', spin: false }
}

// Phase 2 (克隆音频) badge: same derivation against clone_status / cloneRunning / cloneTargets.
function cloneBadge(v: VoiceItem): PhaseBadge {
  const task = cloneRows.value.get(v.name)
  if (task?.status === 'running' && task.progress === 100) {
    if (task.current === '克隆音频已完成') return { label: '已完成', variant: 'success', spin: false }
    if (task.current === '克隆音频失败') return { label: '失败', variant: 'destructive', spin: false }
  }
  if (task && ACTIVE.includes(task.status)) {
    if (task.status === 'paused') return { label: '已暂停', variant: 'warning', spin: false }
    if (task.status === 'cancelling') return { label: '取消中', variant: 'warning', spin: true }
    if (task.status === 'running') return { label: '制作中', variant: 'warning', spin: true }
    return { label: '排队中', variant: 'warning', spin: false }
  }
  if (cloneSubmitting.value && inTargets(cloneTargets.value, v.name)) {
    return { label: '制作中', variant: 'warning', spin: true }
  }
  if (task?.status === 'failed' || task?.status === 'timeout') return { label: '失败', variant: 'destructive', spin: false }
  if (task?.status === 'succeeded') {
    const completion = task.result?.results?.find((r: { speaker: string; ok: boolean }) => r.speaker === v.name)
    if (completion?.ok === false) return { label: '失败', variant: 'destructive', spin: false }
    if (completion?.ok === true) return { label: '已完成', variant: 'success', spin: false }
  }
  if (v.clone_status === 'done') return { label: '已完成', variant: 'success', spin: false }
  if (v.clone_status === 'failed') return { label: '失败', variant: 'destructive', spin: false }
  return { label: '未制作', variant: 'secondary', spin: false }
}

const listPage = useListPage(loadVoices, () => { speakers.value = []; mergeTargetItems.value = []; mergeTotal.value = 0 })
const listPagination = listPage.pagination
async function loadVoices() {
  const request = ++voicesRequest
  const context = `${auth.user?.id || ''}:${project.activeProjectId}:${script}`
  const current = () => request === voicesRequest && context === `${auth.user?.id || ''}:${project.activeProjectId}:${script}`
  voicesLoading.value = true
  voicesLoadError.value = ''
  try {
    const r = await listVoices(script, listPage.query, listPage.begin())
    if (!current()) return
    listPage.received(r.pagination)
    hasScript.value = r.has_script
    speakers.value = r.speakers
  } catch (e: any) {
    if (current()) voicesLoadError.value = e?.message || '角色加载失败，请重试。'
  } finally {
    if (current()) voicesLoading.value = false
  }
}

let progressRefreshTimer: ReturnType<typeof setTimeout> | null = null
let progressRefreshRunning = false
let progressRefreshPending = false
function scheduleProgressRefresh() {
  progressRefreshPending = true
  if (progressRefreshTimer || progressRefreshRunning) return
  progressRefreshTimer = setTimeout(async () => {
    progressRefreshTimer = null
    progressRefreshPending = false
    progressRefreshRunning = true
    try { await loadVoices() }
    finally {
      progressRefreshRunning = false
      if (progressRefreshPending) scheduleProgressRefresh()
    }
  }, 400)
}
function clearProgressRefresh() {
  if (progressRefreshTimer) clearTimeout(progressRefreshTimer)
  progressRefreshTimer = null
  progressRefreshPending = false
}
onBeforeUnmount(clearProgressRefresh)

watch(() => `${auth.user?.id || ''}:${project.activeProjectId}`, () => {
  clearProgressRefresh()
  ++voicesRequest
  speakers.value = []
  foundationBusy.value = false
  foundationSubmitting.value = false
  cloneBusy.value = false
  cloneSubmitting.value = false
  foundationTargets.value = null
  cloneTargets.value = null
  hasScript.value = false
  voicesLoading.value = false
  voicesLoadError.value = ''
  Object.keys(prompts).forEach(key => delete prompts[key])
  closePicker()
  closeMerge()
  closeGenderMenu()
})
onBeforeUnmount(() => { ++voicesRequest })


// 刷新恢复：页面重载后本地 taskId 丢失，但后端任务仍在跑（store 的 refresh 已拉回全量任务）。
// 按 module 重新挂接在途的阶段 1 / 阶段 2 任务——恢复日志面板绑定、按钮门控与完成 watcher
//（行内「生成中/制作中」overlay 本就是 module 派生，刷新后已自行恢复）。
function reattachTasks() {
  const f = taskStore.activeTasks('voices-foundation')[0]
  if (f) {
    foundationBatch.track(taskStore.activeTasks('voices-foundation').map(row => row.id))
    foundationBusy.value = true
  }
  const c = taskStore.activeTasks('voices-clone')[0]
  if (c) {
    cloneBatch.track(taskStore.activeTasks('voices-clone').map(row => row.id))
    cloneBusy.value = true
  }
}

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  try {
    status.value = await ttsStatus()
  } catch {
    status.value = { implemented: false, message: '后端未连接' }
  }
  await loadVoices()
  await taskStore.refresh()
  reattachTasks()
})

let firstActivation = true
onActivated(() => {
  // keep-alive 首次激活 = onMounted 已加载，跳过。之后每次返回本页重取角色列表：
  // 「文本解析」页刚完成的新解析结果（03_parsed_json 更新）必须在这里可见。
  if (firstActivation) {
    firstActivation = false
    return
  }
  void loadVoices()
  void taskStore.refresh().then(() => reattachTasks())
})

async function doFoundations(opts: {
  speakers?: string[]
  new_only?: boolean
  overrides?: Record<string, string>
}) {
  if (foundationBusy.value || cloneRunning.value) return
  const isCurrent = captureScope()
  foundationBusy.value = true
  foundationSubmitting.value = true
  error.value = ''
  foundationResult.value = null
  foundationTargets.value = opts.speakers ?? (opts.new_only
    ? speakers.value.filter(v => v.foundation_status !== 'done').map(v => v.name)
    : null)
  try {
    const submitted = await withinScope(prepareFoundations({ ...opts, script }), isCurrent)
    foundationBatch.track(submitted.task_ids)
    if (!submitted.task_ids.length) foundationBusy.value = false
    await withinScope(taskStore.refresh(), isCurrent)
    // Completion is handled by the watcher on foundationTask.status.
  } catch (e: any) {
    if (!isCurrent() || e?.name === 'AbortError') return
    error.value = e?.message || '启动失败'
    foundationBusy.value = false
  } finally {
    if (isCurrent()) foundationSubmitting.value = false
  }
}

async function doClones(opts: {
  speakers?: string[]
  new_only?: boolean
}) {
  if (cloneBusy.value || foundationRunning.value) return
  const isCurrent = captureScope()
  cloneBusy.value = true
  cloneSubmitting.value = true
  error.value = ''
  cloneResult.value = null
  cloneTargets.value = opts.speakers ?? null
  try {
    const submitted = await withinScope(generateVoiceCandidates({ ...opts, script }), isCurrent)
    cloneBatch.track(submitted.task_ids)
    if (!submitted.task_ids.length) cloneBusy.value = false
    await withinScope(taskStore.refresh(), isCurrent)
    // Completion is handled by the watcher on cloneTask.status.
  } catch (e: any) {
    if (!isCurrent() || e?.name === 'AbortError') return
    error.value = e?.message || '启动失败'
    cloneBusy.value = false
  } finally {
    if (isCurrent()) cloneSubmitting.value = false
  }
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
  return v.preview ? resourcePreviewUrl('04_voice_profiles', v.preview) : ''
}

function candidateUrl(c: { preview: string }): string {
  return c.preview ? resourcePreviewUrl('04_voice_profiles', c.preview) : ''
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
  overlayReturnFocus = document.activeElement as HTMLElement
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
  overlayReturnFocus = document.activeElement as HTMLElement
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
    const r = await mergeSpeakers(src, tgt, script)
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
  const isCurrent = captureScope()
  void foundationBatch.cancel().catch(e => { if (isCurrent()) error.value = e?.message || '取消失败' })
}
function cancelClone() {
  const isCurrent = captureScope()
  void cloneBatch.cancel().catch(e => { if (isCurrent()) error.value = e?.message || '取消失败' })
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
    } else if (st === 'failed' || st === 'timeout') {
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
    if (ACTIVE.includes(t.status)) scheduleProgressRefresh()
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
    } else if (st === 'failed' || st === 'timeout') {
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
    if (ACTIVE.includes(t.status)) scheduleProgressRefresh()
  },
)
</script>

<template>
  <div class="voices-page  viewport-workbench" @keydown="overlayKeydown">
    <header class="page-header mb-3">
      <div>
        <p class="eyebrow">Pipeline · Voices</p>
        <h1 class="page-title flex items-center gap-3">
          角色配音
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
      <WorkbenchContextBar>
        <template #icon><Users /></template>
        <template #title>角色声音核对</template>
        <template #description>选择角色检查声音，使用底部操作生成基础与候选音色。</template>
        <template #metrics>
          <div class="workbench-context-metric"><strong>{{ listPagination?.counts.all ?? speakers.length }}</strong>角色总数</div>
          <div class="workbench-context-metric"><strong class="!text-emerald-600 dark:!text-emerald-400">{{ readyCount }}</strong>音色已就绪</div>
        </template>
      </WorkbenchContextBar>
      <VoicesWorkbench
        remote :pagination="listPagination" @request-page="listPage.request"
        :speakers="speakers" :prompts="prompts" :loading="voicesLoading" :load-error="voicesLoadError" :has-script="hasScript"
        :foundation-blocked="foundationBlocked" :clone-blocked="cloneBlocked" :foundation-busy="foundationBusy"
        :foundation-running="foundationRunning" :clone-running="cloneRunning" :gender-busy="genderBusy"
        :overlay-open="Boolean(activeOverlay)"
        :foundation-badge="foundationBadge" :clone-badge="cloneBadge" :preview-url="previewUrl" :pick-label="pickLabel" :pick-disabled="pickDisabled"
        @refresh="loadVoices" @gender="openGenderMenu" @merge="openMerge" @pick="openPicker"
        @foundation="regenFoundation" @clone="remakeClone" @copy="copyDescriptionToPrompt" @prompt="(name, value) => prompts[name] = value"
      />
      <Card class="voices-production" aria-label="角色声音制作">
        <!-- 阶段 1 · 生成语音推理基础（LLM only） -->
        <section class="voices-stage" aria-labelledby="voices-foundation-title">
          <CardHeader class="p-3 pb-2">
            <CardTitle id="voices-foundation-title" class="text-xs flex items-center gap-2"><Sparkles class="h-4 w-4" />阶段 1 · 生成语音推理基础</CardTitle>
            <CardDescription class="text-xs">
              为角色生成声音描述和种子文案。
            </CardDescription>
          </CardHeader>
          <CardContent class="voices-stage-content space-y-2 p-3 pt-0">
            <LiveLogPanel :task="foundationTask" :max-height-class="'h-40'" />

            <div
              v-if="foundationResult"
              class="flex items-center gap-2 rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-400"
            >
              <CheckCircle2 class="h-4 w-4 shrink-0" />
              完成：为 {{ foundationResult.count }} 个角色生成语音推理基础，识别 {{ foundationResult.aliases }} 个别名（未启动 TTS）。
            </div>
          </CardContent>
        </section>

        <!-- 阶段 2 · 制作克隆音频（TTS only） -->
        <section class="voices-stage" aria-labelledby="voices-clone-title">
          <CardHeader class="p-3 pb-2">
            <CardTitle id="voices-clone-title" class="text-xs flex items-center gap-2"><AudioWaveform class="h-4 w-4" />阶段 2 · 制作克隆音频</CardTitle>
            <CardDescription class="text-xs">
              为角色生成候选音色。请先关闭 LLM，再开始制作。
            </CardDescription>
          </CardHeader>
          <CardContent class="voices-stage-content space-y-2 p-3 pt-0">
            <LiveLogPanel :task="cloneTask" :max-height-class="'h-40'" />

            <div
              v-if="cloneResult"
              class="flex items-center gap-2 rounded-md bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-400"
            >
              <CheckCircle2 class="h-4 w-4 shrink-0" />
              完成：克隆音频成功 {{ cloneResult.ok }} / 失败 {{ cloneResult.failed }} / 共 {{ cloneResult.count }} 个角色。
            </div>
          </CardContent>
        </section>
      </Card>

      <WorkbenchActionBar>
        <template #summary>
          <strong>声音就绪 {{ readyCount }} / {{ listPagination?.counts.all ?? speakers.length }} 个角色</strong>
          <p class="mt-1 text-muted-foreground">基础 {{ foundationDone }} / {{ (listPagination?.counts.non_alias ?? nonAlias.length) }} · 克隆音频 {{ cloneDone }} / {{ (listPagination?.counts.non_alias ?? nonAlias.length) }}</p>
        </template>
        <div class="flex flex-wrap items-center gap-2" role="group" aria-label="基础生成">
          <span class="text-xs text-muted-foreground">基础生成</span>
          <Button :disabled="foundationBlocked" @click="doFoundations({})">
            <Loader2 v-if="foundationBusy" class="h-4 w-4 animate-spin" />
            <Sparkles v-else class="h-4 w-4" />
            {{ foundationBusy ? '生成中…' : '批量生成所有角色' }}
          </Button>
          <Button variant="outline" :disabled="foundationBlocked" @click="doFoundations({ new_only: true })">
            <Users class="h-4 w-4" />仅新增角色
          </Button>
          <Button v-if="foundationTask && ACTIVE.includes(foundationTask.status)" variant="destructive" size="sm" @click="cancelFoundation">取消基础生成</Button>
        </div>
        <div class="flex flex-wrap items-center gap-2" role="group" aria-label="克隆制作">
          <span class="text-xs text-muted-foreground">克隆制作</span>
          <Button :disabled="cloneBlocked" @click="doClones({ new_only: true })">
            <Loader2 v-if="cloneBusy" class="h-4 w-4 animate-spin" />
            <AudioWaveform v-else class="h-4 w-4" />
            {{ cloneBusy ? '制作中…' : '批量制作克隆音频' }}
          </Button>
          <Button v-if="cloneTask && ACTIVE.includes(cloneTask.status)" variant="destructive" size="sm" @click="cancelClone">取消克隆制作</Button>
        </div>
        <Button v-if="hasScript && readyCount >= speakers.length && speakers.length > 0" variant="outline" size="sm" @click="router.push('/batch')">前往音频合成<ArrowRight class="h-4 w-4" /></Button>
      </WorkbenchActionBar>
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
      <div ref="pickerPanel" role="dialog" aria-modal="true" aria-label="选择音色" class="max-h-[80vh] w-full max-w-lg space-y-4 overflow-y-auto rounded-lg border bg-background p-5 shadow-lg">
        <div class="flex items-start justify-between gap-3">
          <div>
            <h2 class="text-lg font-semibold">选择音色 · {{ pickerTarget.name }}</h2>
            <p class="mt-1 text-xs text-muted-foreground">
              试听候选音频并选择最终音色；未选择时使用第 1 条。
            </p>
          </div>
          <Button variant="ghost" size="icon" aria-label="关闭音色选择" :disabled="pickerBusy" @click="closePicker">
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
              data-initial-focus
              :checked="pickerChoice === c.id"
              :disabled="pickerBusy"
              @change="pickerChoice = c.id"
            />
            <span class="min-w-0 flex-1 truncate text-sm">
              候选 #{{ c.id }}
              <span v-if="c.id === '1'" class="ml-1 text-xs text-muted-foreground">（默认）</span>
            </span>
            <WorkbenchStatus v-if="c.id === activeCandidateId(pickerTarget)" variant="success" class="shrink-0">当前</WorkbenchStatus>
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
      <div ref="mergePanel" role="dialog" :aria-modal="!mergeConfirm ? true : undefined" :inert="mergeConfirm || undefined" aria-label="合并角色" class="max-h-[80vh] w-full max-w-lg space-y-4 overflow-y-auto rounded-lg border bg-background p-5 shadow-lg">
        <div class="flex items-start justify-between gap-3">
          <div>
            <h2 class="text-lg font-semibold">合并角色 · {{ mergeSourceItem.name }}</h2>
            <p class="mt-1 text-xs text-muted-foreground">
              将该角色的台词并入所选角色，并移除原角色配置。
            </p>
          </div>
          <Button variant="ghost" size="icon" aria-label="关闭角色合并" :disabled="mergeBusy" @click="closeMerge">
            <X class="h-4 w-4" />
          </Button>
        </div>

        <div class="relative">
          <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            v-model="mergeQuery"
            class="h-9 pl-8 text-sm"
            placeholder="搜索目标角色…"
            aria-label="搜索目标角色"
            data-initial-focus
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
        <Pager :page="mergePage" :page-count="Math.max(1, Math.ceil(mergeTotal / 10))" :total="mergeTotal" :page-size="10" unit="个角色" @update:page="mergePage = $event" />

        <Alert v-if="mergeError" variant="destructive">{{ mergeError }}</Alert>

        <div class="flex items-center justify-end gap-2">
          <Button variant="outline" size="sm" :disabled="mergeBusy" @click="closeMerge">取消</Button>
          <Button :disabled="mergeBusy || !mergeTarget" @click="openMergeConfirm">
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
      <div ref="mergeConfirmPanel" role="dialog" aria-modal="true" aria-label="确认合并" class="w-full max-w-md space-y-4 rounded-lg border bg-background p-5 shadow-lg">
        <h2 class="text-lg font-semibold">确认合并？</h2>
        <p class="flex items-center gap-2 text-sm">
          <span class="font-medium">{{ mergeSourceItem?.name }}</span>
          <ArrowRight class="h-3.5 w-3.5" />
          <span class="font-medium">{{ mergeTarget }}</span>
        </p>
        <ul class="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
          <li>替换 {{ mergeSourceItem?.name }} 的 {{ mergeSourceItem?.line_count ?? 0 }} 条台词（当前范围：
            全部已解析章节）。</li>
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

<style scoped>
.voices-page :deep(.page-header) { margin-bottom:12px; }
.voices-production { overflow:hidden; border-radius:12px; }
.voices-stage + .voices-stage { position:relative; }
.voices-stage + .voices-stage::before { position:absolute; top:0; right:12px; left:12px; height:1px; background:hsl(var(--border) / .65); content:''; }
@media(min-width:881px) and (min-height:700px) {
  .voices-page { display:flex; flex-direction:column; height:100%; }
  .voices-page > :not(.voice-workbench) { flex-shrink:0; }
  .voices-page :deep(.voice-workbench) { flex:1; min-height:180px; }
  .voices-page :deep(.voice-list), .voices-page :deep(.voice-detail) { min-height:0; }
  .voices-page :deep(.voice-scroll) { min-height:0; max-height:none; }
  .voices-page :deep(.voice-detail) { max-height:none; }
  .voices-stage { display:grid; grid-template-columns:160px minmax(0,1fr); align-items:center; }
  .voices-stage-content { padding-top:12px; max-height:clamp(88px, 14dvh, 128px); overflow-y:auto; }
}
.voices-stage :deep(button), .voices-footer :deep(button) { min-height:32px; height:32px; font-size:12px; }
.voices-stage :deep(label), .voices-stage :deep(.text-sm) { font-size:12px; }

.voices-stage :deep(h3) { font-size:12px; }
@media(min-width:1200px) { .voices-stage { display:grid; grid-template-columns:200px minmax(0,1fr); align-items:center; } .voices-stage-content { padding-top:12px; } }
@media(pointer:coarse) { .voices-stage :deep(button), .voices-footer :deep(button) { min-height:44px; height:auto; } }
</style>
