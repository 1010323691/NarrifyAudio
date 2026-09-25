<script setup lang="ts">
// 音乐库（全局资源，工作空间外、跨工程共享）——曲目上传 / 试听 / 打标 / AI 推荐 /
// 批量操作 / 标签管理。页面不经 ProjectGateAlert（与工作空间无关）。
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'
import { useToast } from '@/components/ui/toast'
import { showConfirm, showPrompt } from '@/components/ui/dialog'
import { useTaskStore } from '@/stores/task'
import { useLabelDerivedTasks, LABEL_TASK_TERMINAL_STATUSES } from '@/composables/useLabelDerivedTasks'
import {
  applySuggestions,
  batchDelete,
  batchEnable,
  batchTags,
  createFolder,
  createTag,
  deleteFolder,
  deleteTag,
  deleteTrack,
  getLibrary,
  moveTracks,
  musicPreviewUrl,
  renameFolder,
  renameTag,
  suggestTags,
  suggestTagsBatch,
  updateTrack,
  uploadMusic,
} from '@/api/music'
import { pickFiles } from '@/utils/fileops'
import { formatDuration } from '@/utils/format'
import type { MusicLibrary, MusicSuggestion, MusicTagCategory, TaskSnapshot, TrackTags } from '@/types'

import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import Badge from '@/components/ui/Badge.vue'
import Alert from '@/components/ui/Alert.vue'
import Input from '@/components/ui/Input.vue'
import Textarea from '@/components/ui/Textarea.vue'
import Switch from '@/components/ui/Switch.vue'
import Table from '@/components/ui/Table.vue'
import TableBody from '@/components/ui/TableBody.vue'
import TableCell from '@/components/ui/TableCell.vue'
import TableHead from '@/components/ui/TableHead.vue'
import TableHeader from '@/components/ui/TableHeader.vue'
import TableRow from '@/components/ui/TableRow.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import {
  BadgeCheck,
  ChevronRight,
  Disc3,
  Folder,
  FolderInput,
  Loader2,
  Music,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Tags,
  Trash2,
  Upload,
  X,
} from 'lucide-vue-next'

const { push: toast } = useToast()

// ---------------------------------------------------------------------------
// 常量（标签四桶 + 展示名 + 颜色）
// ---------------------------------------------------------------------------

const CATEGORIES: MusicTagCategory[] = ['scene', 'mood', 'emotion', 'custom']
const CAT_LABEL: Record<MusicTagCategory, string> = {
  scene: '场景',
  mood: '气氛',
  emotion: '情绪',
  custom: '自定义',
}
const CAT_BADGE: Record<MusicTagCategory, string> = {
  scene: 'border-sky-500/30 bg-sky-500/15 text-sky-600 dark:text-sky-400',
  mood: 'border-violet-500/30 bg-violet-500/15 text-violet-600 dark:text-violet-400',
  emotion: 'border-rose-500/30 bg-rose-500/15 text-rose-600 dark:text-rose-400',
  custom: 'border-amber-500/30 bg-amber-500/15 text-amber-600 dark:text-amber-400',
}

// ---------------------------------------------------------------------------
// 数据
// ---------------------------------------------------------------------------

const lib = ref<MusicLibrary | null>(null)
const loading = ref(false)
const loadError = ref('')
const uploading = ref(false)
const search = ref('')
const tagFilter = ref('') // '' = 全部；否则 "category:name"
const selected = reactive<Record<string, boolean>>({})

// ---------------------------------------------------------------------------
// 视图（root = 文件夹行 + 未分类曲目；all = 全部曲目；folder = 单个文件夹）
// 页面本地状态：keep-alive 重新进入时回 root（onActivated），无跨导航持久。
// 文件夹 = 索引元数据（music_index.json 的 folders 段），非物理子目录——
// 匹配/引用只依赖裸文件名与标签，文件夹不参与 LLM 匹配。
// ---------------------------------------------------------------------------

type View = { kind: 'root' } | { kind: 'all' } | { kind: 'unclassified' } | { kind: 'folder'; name: string }
const view = ref<View>({ kind: 'all' })
const folderSort = ref<'name' | 'created_at' | 'count'>('name')
const folderBusy = ref(false)
const folderDialogOpen = ref(false)
const folderDraft = ref('')
const folderDeleteTarget = ref<string | null>(null)

function goRoot() {
  view.value = { kind: 'all' }
}
function goAll() {
  view.value = { kind: 'all' }
}
function goFolder(name: string) {
  view.value = { kind: 'folder', name }
}
function goUnclassified() {
  view.value = { kind: 'unclassified' }
}

const folders = computed(() => {
  const raw = lib.value?.folders ?? {}
  return (Object.keys(raw).sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'))).map((n) => ({
    name: n,
    created_at: raw[n]?.created_at ?? '',
  }))
})

const sortedFolders = computed(() => {
  const counts = lib.value?.folder_counts ?? {}
  const list = [...folders.value]
  if (folderSort.value === 'created_at') {
    list.sort((a, b) => a.created_at.localeCompare(b.created_at) || a.name.localeCompare(b.name, 'zh-Hans-CN'))
  } else if (folderSort.value === 'count') {
    list.sort((a, b) => (counts[b.name] ?? 0) - (counts[a.name] ?? 0) || a.name.localeCompare(b.name, 'zh-Hans-CN'))
  } else {
    list.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'))
  }
  return list
})

function folderCount(name: string): number {
  const c = lib.value?.folder_counts?.[name]
  if (typeof c === 'number') return c
  // folder_counts 缺失时按 track.folder 兜底计数。
  return Object.values(lib.value?.tracks ?? {}).filter((t) => t.folder === name).length
}

/** 移动目标选项（行内 / 批量共用）：全部文件夹 + 未分类。 */
const folderOptions = computed(() => [
  { value: '', label: '未分类' },
  ...folders.value.map((f) => ({ value: f.name, label: f.name })),
])

/** 音频格式（扩展名，从文件名派生；零后端改动）。 */
function extOf(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toUpperCase() : ''
}

async function refresh() {
  loading.value = true
  loadError.value = ''
  try {
    lib.value = await getLibrary()
    // 清掉已消失曲目的勾选（仅 UI 状态，不触碰任何数据）。
    const names = new Set(Object.keys(lib.value.tracks))
    for (const k of Object.keys(selected)) if (!names.has(k)) delete selected[k]
  } catch (e: any) {
    loadError.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

const allTracks = computed(() =>
  Object.entries(lib.value?.tracks ?? {}).sort((a, b) => a[0].localeCompare(b[0])),
)
const trackList = computed(() => {
  // 视图前置过滤（搜索/标签过滤作用于「当前视图内曲目」，现有逻辑不动）。
  let list = allTracks.value
  const folderName = view.value.kind === 'folder' ? view.value.name : null
  if (folderName !== null) list = list.filter(([, t]) => t.folder === folderName)
  else if (view.value.kind === 'unclassified') list = list.filter(([, t]) => !t.folder)
  const q = search.value.trim().toLowerCase()
  if (q) list = list.filter(([n]) => n.toLowerCase().includes(q))
  if (tagFilter.value) {
    const [cat, ...rest] = tagFilter.value.split(':')
    const tag = rest.join(':')
    list = list.filter(
      ([, t]) => Array.isArray(t.tags?.[cat as MusicTagCategory]) && t.tags[cat as MusicTagCategory].includes(tag),
    )
  }
  return list
})
const enabledCount = computed(() => allTracks.value.filter(([, t]) => t.enabled).length)

const filterOptions = computed(() => {
  const out: { value: string; label: string }[] = []
  for (const c of CATEGORIES) {
    for (const t of lib.value?.tags?.[c] ?? []) out.push({ value: `${c}:${t}`, label: `${CAT_LABEL[c]} · ${t}` })
  }
  return out
})

// ---------------------------------------------------------------------------
// 多选
// ---------------------------------------------------------------------------

const visibleNames = computed(() => trackList.value.map(([n]) => n))
const allVisibleSelected = computed(
  () => visibleNames.value.length > 0 && visibleNames.value.every((n) => !!selected[n]),
)
const selectedNames = computed(() => Object.keys(selected))

function toggleSelectAll(e: Event) {
  const on = (e.target as HTMLInputElement).checked
  for (const n of visibleNames.value) {
    if (on) selected[n] = true
    else delete selected[n]
  }
}
function clearSelection() {
  for (const k of Object.keys(selected)) delete selected[k]
}

// ---------------------------------------------------------------------------
// AI 识别任务派生（module music-ai-tags，label「AI 推荐标签：{name}」）
// ---------------------------------------------------------------------------

const taskStore = useTaskStore()
const AI_MODULE = 'music-ai-tags'
const aiTasks = useLabelDerivedTasks(AI_MODULE)

function suggestionOf(name: string): MusicSuggestion | undefined {
  return lib.value?.suggestions?.[name]
}
function suggestionHasTags(name: string): boolean {
  const s = suggestionOf(name)
  return !!s && CATEGORIES.some((c) => (s.tags?.[c]?.length ?? 0) > 0)
}
function suggestionSummary(name: string): string {
  const s = suggestionOf(name)
  if (!s) return ''
  const parts = CATEGORIES.flatMap((c) => s.tags?.[c] ?? [])
  return parts.length ? parts.join('、') : '（无标签）'
}

// ---------------------------------------------------------------------------
// 上传（逐文件上传，逐文件拿 409/400 语义；归属 = 当前视图）
// 文件夹内上传 = 文件永久归属该文件夹（索引 folder 字段持久化）；
// root / all 视图上传 = 未分类。拖拽上传复用同一循环（onDrop 传 files）。
// ---------------------------------------------------------------------------

const uploadTarget = computed(() => (view.value.kind === 'folder' ? view.value.name : ''))

async function doUpload(files?: File[]) {
  if (uploading.value) return
  let list: File[]
  if (files && files.length) {
    list = files
  } else {
    const picked = await pickFiles()
    if (!picked.length) return
    list = picked
  }
  uploading.value = true
  try {
    let ok = 0
    for (const f of list) {
      try {
        await uploadMusic(f, uploadTarget.value || undefined)
        ok++
        // 实时：每首上传完成立即刷新列表（不等整批结束）。
        await refresh()
      } catch (e: any) {
        toast({
          title: '上传失败',
          variant: 'destructive',
          description: `${f.name}：${e?.message || '未知错误'}`,
        })
      }
    }
    if (ok)
      toast({
        title: '上传完成',
        variant: 'success',
        description: `成功 ${ok} / ${list.length} 首${uploadTarget.value ? `（已归属「${uploadTarget.value}」）` : ''}`,
      })
  } finally {
    uploading.value = false
  }
}

// 拖拽上传（Card 级；dragenter/leave 计数防子元素抖动）：只收 mp3/wav/flac。
const dragOver = ref(false)
let dragDepth = 0
function onDragEnter(e: DragEvent) {
  e.preventDefault()
  dragDepth++
  dragOver.value = true
}
function onDragLeave(e: DragEvent) {
  e.preventDefault()
  dragDepth = Math.max(0, dragDepth - 1)
  if (!dragDepth) dragOver.value = false
}
function onDragOver(e: DragEvent) {
  e.preventDefault()
}
function onDrop(e: DragEvent) {
  dragOver.value = false
  dragDepth = 0
  const files = Array.from(e.dataTransfer?.files ?? []).filter((f) => /\.(mp3|wav|flac)$/i.test(f.name))
  if (!files.length) {
    toast({ title: '无可上传的音频文件', variant: 'default', description: '仅支持 mp3 / wav / flac。' })
    return
  }
  void doUpload(files)
}

// ---------------------------------------------------------------------------
// 行操作（启用开关 = 乐观更新；删除 / 批量）
// ---------------------------------------------------------------------------

async function toggleEnabled(name: string, value: boolean) {
  const tr = lib.value?.tracks[name]
  if (!tr) return
  const prev = tr.enabled
  tr.enabled = value
  try {
    const r = await updateTrack(name, { enabled: value })
    lib.value!.tracks[name] = r.track
  } catch (e: any) {
    tr.enabled = prev
    toast({ title: '操作失败', variant: 'destructive', description: e?.message || '保存失败' })
  }
}

async function doDeleteOne(name: string) {
  if (!await showConfirm(`删除音乐「${name}」？（音乐文件与索引条目都会被删除）`, { title: '删除音乐', destructive: true })) return
  try {
    const r = await deleteTrack(name)
    if (r.skipped.length) {
      toast({
        title: '未删除（被锁定引用）',
        variant: 'default',
        description: r.skipped.map((s) => `${s.name}：${s.reason}`).join('；'),
      })
    } else {
      toast({ title: '已删除', variant: 'success', description: name })
    }
    delete selected[name]
    await refresh()
  } catch (e: any) {
    toast({ title: '删除失败', variant: 'destructive', description: e?.message || '' })
  }
}

// ---------------------------------------------------------------------------
// 文件夹管理（索引元数据：新建 / 重命名 / 删除；成功后统一 refresh()）
// 非空文件夹禁止删除（后端 409 兜底；UI 按钮禁用 + title 提示）。
// ---------------------------------------------------------------------------

function openCreateFolderDialog() {
  if (folderBusy.value) return
  folderDraft.value = ''
  folderDialogOpen.value = true
}

function fileSize(value?: number | null): string {
  if (value == null) return '未找到文件'
  return value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`
}

function closeCreateFolderDialog() {
  if (folderBusy.value) return
  folderDialogOpen.value = false
}

async function doCreateFolder() {
  const name = folderDraft.value.trim()
  if (!name) {
    toast({ title: '请输入收藏集名称', variant: 'default' })
    return
  }
  if (folderBusy.value) return
  folderBusy.value = true
  try {
    await createFolder(name)
    folderDialogOpen.value = false
    folderDraft.value = ''
    toast({ title: '已创建文件夹', variant: 'success', description: name })
    await refresh()
  } catch (e: any) {
    toast({ title: '新建文件夹失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    folderBusy.value = false
  }
}

async function doRenameFolder(name: string) {
  const v = await showPrompt(`把文件夹「${name}」改名为：`, name, { title: '重命名收藏集', inputLabel: '收藏集名称' })
  if (v == null) return
  const nv = v.trim()
  if (!nv || nv === name) return
  if (folderBusy.value) return
  folderBusy.value = true
  try {
    const r = await renameFolder(name, nv)
    if (view.value.kind === 'folder' && view.value.name === name) view.value = { kind: 'folder', name: nv }
    toast({
      title: '已改名',
      variant: 'success',
      description: r.renamed_tracks ? `波及 ${r.renamed_tracks} 首音乐的文件夹归属` : '（无曲目归属变化）',
    })
    await refresh()
  } catch (e: any) {
    toast({ title: '文件夹改名失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    folderBusy.value = false
  }
}

function openDeleteFolderDialog(name: string) {
  if (folderBusy.value || folderCount(name) > 0) return
  folderDeleteTarget.value = name
}

function closeDeleteFolderDialog() {
  if (folderBusy.value) return
  folderDeleteTarget.value = null
}

async function confirmDeleteFolder() {
  const name = folderDeleteTarget.value
  if (!name || folderCount(name) > 0) return
  if (folderBusy.value) return
  folderBusy.value = true
  try {
    await deleteFolder(name)
    folderDeleteTarget.value = null
    if (view.value.kind === 'folder' && view.value.name === name) goRoot()
    toast({ title: '已删除文件夹', variant: 'success', description: name })
    await refresh()
  } catch (e: any) {
    toast({ title: '删除文件夹失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    folderBusy.value = false
  }
}

// 行内移动（FolderInput 图标 → 内联 select：全部文件夹 + 未分类）。
const moveOpenFor = ref<string | null>(null)
const moveTo = ref('')
function openMove(name: string) {
  moveOpenFor.value = name
  moveTo.value = ''
}
function closeMove() {
  moveOpenFor.value = null
}
async function doMoveOne(name: string) {
  if (moveOpenFor.value !== name) return
  const target = moveTo.value
  moveOpenFor.value = null
  try {
    const r = await moveTracks([name], target)
    if (r.missing.length) {
      toast({ title: '部分曲目不存在', variant: 'default', description: r.missing.join('、') })
      return
    }
    // 移出入当前文件夹的行由 refresh 后的视图过滤自然消失/补位。
    await refresh()
  } catch (e: any) {
    toast({ title: '移动失败', variant: 'destructive', description: e?.message || '' })
  }
}

// ---------------------------------------------------------------------------
// 批量操作条
// ---------------------------------------------------------------------------

const batchCat = ref<MusicTagCategory>('mood')
const batchTag = ref('')
const batchBusy = ref(false)

async function doBatchTag(op: 'add' | 'remove') {
  const names = selectedNames.value
  if (batchBusy.value || !names.length) return
  if (!batchTag.value) {
    toast({ title: '请先在标签下拉中选择标签', variant: 'default' })
    return
  }
  batchBusy.value = true
  try {
    await batchTags(names, [batchTag.value], batchCat.value, op)
    await refresh()
  } catch (e: any) {
    toast({ title: '批量打标失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    batchBusy.value = false
  }
}

async function doBatchEnable(enabled: boolean) {
  const names = selectedNames.value
  if (batchBusy.value || !names.length) return
  batchBusy.value = true
  try {
    const r = await batchEnable(names, enabled)
    await refresh()
    if (r.missing.length) {
      toast({
        title: '部分曲目不存在',
        variant: 'default',
        description: r.missing.slice(0, 3).join('、') + (r.missing.length > 3 ? ' …' : ''),
      })
    }
  } catch (e: any) {
    toast({ title: '操作失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    batchBusy.value = false
  }
}

async function doBatchDelete() {
  const names = selectedNames.value
  if (batchBusy.value || !names.length) return
  if (!await showConfirm(`删除选中的 ${names.length} 首音乐？`, { title: '批量删除音乐', destructive: true })) return
  batchBusy.value = true
  try {
    const r = await batchDelete(names)
    if (r.deleted.length) toast({ title: '已删除', variant: 'success', description: `${r.deleted.length} 首` })
    if (r.skipped.length) {
      toast({
        title: '部分被锁定引用未删除',
        variant: 'default',
        description: `${r.skipped.length} 首：${r.skipped.map((s) => s.name).slice(0, 3).join('、')}${r.skipped.length > 3 ? ' …' : ''}`,
      })
    }
    clearSelection()
    await refresh()
  } catch (e: any) {
    toast({ title: '批量删除失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    batchBusy.value = false
  }
}

// 批量移动（select：全部文件夹 + 未分类；移动可逆、无确认对话框）。
const batchMoveTo = ref('')
async function doBatchMove() {
  const names = selectedNames.value
  if (batchBusy.value || !names.length) return
  batchBusy.value = true
  try {
    const r = await moveTracks(names, batchMoveTo.value)
    if (r.moved.length) {
      toast({
        title: '已移动',
        variant: 'success',
        description: `${r.moved.length} 首 → ${batchMoveTo.value || '未分类'}`,
      })
      clearSelection()
    }
    if (r.missing.length) {
      toast({
        title: '部分曲目不存在',
        variant: 'default',
        description: r.missing.slice(0, 3).join('、') + (r.missing.length > 3 ? ' …' : ''),
      })
    }
    await refresh()
  } catch (e: any) {
    toast({ title: '批量移动失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    batchBusy.value = false
  }
}

// AI 一键识别所选（每曲一任务，共享 LLM 闸）：未手动打标的曲目识别完成后
// 自动采用 AI 结果；已手动打标的保留候选待确认；失败行可重试（同一任务 id）。
const aiBatchBusy = ref(false)
async function doAiSuggestBatch() {
  const names = selectedNames.value
  if (aiBatchBusy.value || !names.length) return
  aiBatchBusy.value = true
  try {
    const r = await suggestTagsBatch(names)
    // 实时：SSE 流在全部任务终态时已关闭——启动后立即同步任务表，
    // 新 PENDING/RUNNING 壳即刻挂进行（「识别中…」），并随 hasActive 重开流。
    await taskStore.refresh()
    toast({
      title: `AI 识别已启动（${r.tracks.length} 首）`,
      variant: 'default',
      description: '未手动打标的曲目识别完成后自动采用 AI 结果；已手动打标的保留候选（「AI 推荐采用」或编辑弹层确认）。仅依据文件名 + 描述，LLM 不读取音频。',
    })
  } catch (e: any) {
    toast({ title: 'AI 识别启动失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    aiBatchBusy.value = false
  }
}
function cancelAiTask(name: string) {
  const t = aiTasks.value.active.get(name)
  if (t) void taskStore.control(t.id, 'cancel')
}
function retryAiTask(name: string) {
  const t = aiTasks.value.failed.get(name)
  if (t) void taskStore.control(t.id, 'retry')
}

// AI 推荐采用：一键采用「有 AI 候选 且 未打任何标签」的选中曲目（绝不覆盖
// 人工已选标签）；采用后消费该曲候选（与弹层确认同语义）。
const aiApplyBusy = ref(false)
function trackHasTags(name: string): boolean {
  const tr = lib.value?.tracks[name]
  return !!tr && CATEGORIES.some((c) => (tr.tags?.[c]?.length ?? 0) > 0)
}
const adoptableNames = computed(() =>
  selectedNames.value.filter((n) => suggestionHasTags(n) && !trackHasTags(n)),
)
async function doAiApply() {
  const names = selectedNames.value
  if (aiApplyBusy.value || !adoptableNames.value.length) return
  aiApplyBusy.value = true
  try {
    const r = await applySuggestions(names)
    await refresh()
    const parts: string[] = []
    if (r.applied.length) parts.push(`已采用 ${r.applied.length} 首`)
    if (r.skipped_manual.length) parts.push(`${r.skipped_manual.length} 首已有手动标签（未覆盖）`)
    if (r.missing.length) parts.push(`${r.missing.length} 首不存在`)
    toast({
      title: r.applied.length ? 'AI 推荐已采用' : '无可采用的 AI 推荐',
      variant: r.applied.length ? 'success' : 'default',
      description: parts.join('；') || '选中曲目没有 AI 候选，或已有手动标签。',
    })
  } catch (e: any) {
    toast({ title: 'AI 推荐采用失败', variant: 'destructive', description: e?.message || '' })
  } finally {
    aiApplyBusy.value = false
  }
}

// ---------------------------------------------------------------------------
// 标签编辑弹层（页内自写 overlay，Voices.vue 先例）
// ---------------------------------------------------------------------------

interface EditorState {
  name: string
  tags: TrackTags
  desc: string
}
const editor = ref<EditorState | null>(null)
const editorSaving = ref(false)
const aiLoading = ref(false)
const aiTags = ref<TrackTags | null>(null)
const aiNote = ref('')
const customDraft = ref('')

function emptyTags(): TrackTags {
  return { scene: [], mood: [], emotion: [], custom: [] }
}
function ensureTags(t: Partial<Record<string, string[]>> | null | undefined): TrackTags {
  const out = emptyTags()
  if (t) {
    for (const c of CATEGORIES) {
      const v = t[c]
      if (Array.isArray(v)) out[c] = v.filter((x): x is string => typeof x === 'string')
    }
  }
  return out
}

function openEditor(name: string) {
  const tr = lib.value?.tracks[name]
  if (!tr) return
  editor.value = { name, tags: ensureTags(tr.tags), desc: tr.description || '' }
  aiTags.value = null
  aiNote.value = ''
  customDraft.value = ''
  // 批量 AI 已产出候选（suggestions 缓存）→ 直接预填，不再调用 LLM；
  // 点「AI 推荐」按钮仍可随时重新生成（覆盖预填）。
  const s = suggestionOf(name)
  if (s && CATEGORIES.some((c) => (s.tags?.[c]?.length ?? 0) > 0)) {
    aiTags.value = ensureTags(s.tags)
    aiNote.value = '已生成标签建议，请选择后保存。'
  }
}
function closeEditor() {
  editor.value = null
}
function toggleEditorTag(cat: MusicTagCategory, tag: string) {
  const e = editor.value
  if (!e) return
  const i = e.tags[cat].indexOf(tag)
  if (i >= 0) e.tags[cat].splice(i, 1)
  else e.tags[cat].push(tag)
}
function addCustomTag() {
  const e = editor.value
  if (!e) return
  const v = customDraft.value.trim()
  if (!v) return
  if (!e.tags.custom.includes(v)) e.tags.custom.push(v)
  customDraft.value = ''
}

// AI 推荐 = 文件名 + 用户描述 + 词表（LLM 不读音频）；结果只是候选，勾选后保存才生效。
async function doSuggest() {
  const e = editor.value
  if (!e || aiLoading.value) return
  aiLoading.value = true
  aiTags.value = null
  aiNote.value = ''
  try {
    const submitted = await suggestTags(e.name, e.desc.trim() || undefined)
    await taskStore.refresh()
    let task: TaskSnapshot | undefined
    for (let attempt = 0; attempt < 1200; attempt += 1) {
      task = taskStore.tasks.find((item) => item.id === submitted.task_id)
      if (task && LABEL_TASK_TERMINAL_STATUSES.has(task.status)) break
      await new Promise((resolve) => setTimeout(resolve, 500))
    }
    if (!task || task.status !== 'succeeded') {
      throw new Error(task?.error || 'AI 推荐任务未完成')
    }
    aiTags.value = ensureTags(task.result?.tags)
    aiNote.value = '已生成标签建议，请选择后保存。'
  } catch (err: any) {
    toast({ title: 'AI 推荐失败', variant: 'destructive', description: err?.message || '' })
  } finally {
    aiLoading.value = false
  }
}
function adoptAi() {
  const e = editor.value
  const s = aiTags.value
  if (!e || !s) return
  for (const c of CATEGORIES) for (const t of s[c]) if (!e.tags[c].includes(t)) e.tags[c].push(t)
  aiTags.value = null
}

async function saveEditor() {
  const e = editor.value
  if (!e || editorSaving.value) return
  editorSaving.value = true
  try {
    const r = await updateTrack(e.name, { tags: e.tags, description: e.desc })
    lib.value!.tracks[e.name] = r.track
    closeEditor()
    toast({ title: '已保存', variant: 'success', description: e.name })
  } catch (err: any) {
    toast({ title: '保存失败', variant: 'destructive', description: err?.message || '' })
  } finally {
    editorSaving.value = false
  }
}

// ---------------------------------------------------------------------------
// 标签管理（注册表：新增 / 改名 / 删除；改名/删除波及全部曲目与分析缓存）
// ---------------------------------------------------------------------------

const newTag = reactive<Record<MusicTagCategory, string>>({ scene: '', mood: '', emotion: '', custom: '' })
const tagInfo = ref<{ cat: MusicTagCategory; name: string; count: number } | null>(null)

function usageCount(cat: MusicTagCategory, name: string): number {
  return Object.values(lib.value?.tracks ?? {}).filter(
    (t) => Array.isArray(t.tags?.[cat]) && t.tags[cat].includes(name),
  ).length
}
function showTagInfo(cat: MusicTagCategory, name: string) {
  tagInfo.value = { cat, name, count: usageCount(cat, name) }
}

async function doAddTag(cat: MusicTagCategory) {
  const v = newTag[cat].trim()
  if (!v) return
  try {
    const r = await createTag(cat, v)
    if (lib.value) lib.value.tags = r.tags
    newTag[cat] = ''
  } catch (e: any) {
    toast({ title: '新增标签失败', variant: 'destructive', description: e?.message || '' })
  }
}

async function doRenameTag(cat: MusicTagCategory, name: string) {
  const v = await showPrompt(`把「${name}」改名为：`, name, { title: '重命名标签', inputLabel: '标签名称' })
  if (v == null) return
  const nv = v.trim()
  if (!nv) return
  try {
    const r = await renameTag(cat, name, nv)
    if (lib.value) lib.value.tags = r.tags
    toast({ title: '已改名', variant: 'success', description: `波及 ${r.affected_tracks} 首音乐的分析与标签` })
    await refresh()
  } catch (e: any) {
    toast({ title: '改名失败', variant: 'destructive', description: e?.message || '' })
  }
}

async function doDeleteTag(cat: MusicTagCategory, name: string) {
  if (!await showConfirm(`删除标签「${name}」？\n仅移除标签（注册表 + 全部音乐 + 章节分析缓存），不删除音乐文件。`, { title: '删除标签', destructive: true })) return
  try {
    const r = await deleteTag(cat, name)
    if (lib.value) lib.value.tags = r.tags
    tagInfo.value = null
    toast({ title: '已删除标签', variant: 'success', description: `${CAT_LABEL[cat]} · ${name}` })
    await refresh()
  } catch (e: any) {
    toast({ title: '删除标签失败', variant: 'destructive', description: e?.message || '' })
  }
}

// ---------------------------------------------------------------------------
// SSE 驱动（getter 式 watch）：AI 任务终态 → 重拉库口径（suggestions 候选）。
// processed 集合防重复（快照重放 / 断线重连）；转回非终态（重试）时释放。
// 批量 N 首不逐条弹 toast（N 大时刷屏）——行状态徽章即反馈。
// ---------------------------------------------------------------------------
const aiProcessed = new Set<string>()
let aiWatcherArmed = false

// 章节气氛分析（bgm-analysis）成功也会把新标签自动登记进全局词表——
// 本页同样刷新，让新词即时可见。
const BGM_ANALYSIS_MODULE = 'bgm-analysis'
watch(
  () =>
    taskStore.tasks
      .filter((t) => t.module === AI_MODULE || t.module === BGM_ANALYSIS_MODULE)
      .map((t) => `${t.id}:${t.status}`)
      .join('|'),
  () => {
    if (!aiWatcherArmed) {
      aiWatcherArmed = true
      for (const t of taskStore.tasks) {
        if ((t.module === AI_MODULE || t.module === BGM_ANALYSIS_MODULE)
            && LABEL_TASK_TERMINAL_STATUSES.has(t.status)) aiProcessed.add(t.id)
      }
      return
    }
    let dirty = false
    for (const t of taskStore.tasks) {
      if (t.module !== AI_MODULE && t.module !== BGM_ANALYSIS_MODULE) continue
      if (!LABEL_TASK_TERMINAL_STATUSES.has(t.status)) {
        aiProcessed.delete(t.id)
        continue
      }
      if (aiProcessed.has(t.id)) continue
      aiProcessed.add(t.id)
      if (t.status === 'succeeded') dirty = true
    }
    if (dirty) void refresh()
  },
)

// ---------------------------------------------------------------------------
// 生命周期（keep-alive 缓存页：重新进入时刷新；F5 后 taskStore.refresh() 使
// 在途 AI 任务按 label 派生重挂，无本地 job 列表）
// ---------------------------------------------------------------------------

// 手动刷新 = 库口径 + 任务口径（不依赖 F5：在途 AI 任务同样重新挂回行状态）。
async function refreshAll() {
  await Promise.all([taskStore.refresh(), refresh()])
}

onMounted(async () => {
  await taskStore.refresh()
  void refresh()
})
onActivated(() => {
  goRoot() // 重新进入回 root 视图（页面本地视图态无跨导航持久需求）
  if (lib.value) void refreshAll()
})
</script>

<template>
  <div class="space-y-4">
    <header class="page-header mb-5">
      <div>
        <p class="eyebrow">Music Library</p>
        <h1 class="page-title flex items-center gap-3"><Disc3 class="h-6 w-6" />音乐库</h1>
        <p class="page-description">管理背景音乐曲目、标签和文件夹。</p>
      </div>
    </header>

    <Card
      :class="dragOver ? 'ring-2 ring-primary' : ''"
      @dragenter="onDragEnter"
      @dragleave="onDragLeave"
      @dragover="onDragOver"
      @drop="onDrop"
    >
      <CardHeader>
        <CardTitle class="flex items-center gap-2">
          <Disc3 class="h-5 w-5" />
          <!-- 面包屑：音乐库（回 root）/ 当前视图名 -->
          <nav class="flex items-center gap-1.5" aria-label="视图路径">
            <button
              type="button"
              class="font-semibold hover:underline"
              :class="view.kind === 'root' ? 'pointer-events-none' : ''"
              @click="goRoot"
            >
              音乐库
            </button>
            <template v-if="view.kind !== 'root'">
              <ChevronRight class="h-3.5 w-3.5 text-muted-foreground" />
              <span>{{ view.kind === 'all' ? '全部音乐' : view.kind === 'unclassified' ? '未分类' : view.name }}</span>
            </template>
          </nav>
        </CardTitle>
        <CardDescription>
          批量管理曲目，或根据文件名和描述推荐标签。文件夹用于分类，匹配仅使用标签。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <Alert v-if="loadError" variant="destructive">
          {{ loadError }}
        </Alert>

        <div v-else-if="allTracks.length" class="grid gap-4 xl:grid-cols-[13rem_minmax(0,1fr)]">
          <aside class="space-y-3 xl:sticky xl:top-4 xl:self-start">
            <div class="rounded-lg border bg-muted/20 p-2">
              <p class="px-2 pb-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">资源视图</p>
              <button type="button" class="library-view-item" :class="view.kind === 'all' ? 'library-view-item-active' : ''" @click="goAll"><Music class="h-4 w-4" />全部音乐 <span>{{ allTracks.length }}</span></button>
              <button type="button" class="library-view-item" :class="view.kind === 'unclassified' ? 'library-view-item-active' : ''" @click="goUnclassified"><Folder class="h-4 w-4" />未分类 <span>{{ allTracks.length - folders.reduce((total, f) => total + folderCount(f.name), 0) }}</span></button>
            </div>
            <div class="rounded-lg border p-2">
              <div class="flex items-center justify-between px-2 pb-1.5"><p class="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">收藏集</p><Button variant="ghost" size="sm" class="h-7 w-7 p-0" title="新建收藏集" :disabled="folderBusy" @click="openCreateFolderDialog"><Plus class="h-4 w-4" /></Button></div>
              <div v-for="f in sortedFolders" :key="f.name" class="group flex min-w-0 items-center rounded-md hover:bg-accent">
                <button
                  type="button"
                  class="library-view-item min-w-0 flex-1"
                  :class="view.kind === 'folder' && view.name === f.name ? 'library-view-item-active' : ''"
                  @click="goFolder(f.name)"
                >
                  <Folder class="h-4 w-4 shrink-0" />
                  <span class="truncate">{{ f.name }}</span>
                  <span>{{ folderCount(f.name) }}</span>
                </button>
                <Button
                  variant="ghost"
                  size="sm"
                  class="mr-0.5 h-8 w-8 shrink-0 p-0 text-muted-foreground hover:text-destructive disabled:opacity-40"
                  :title="
                    folderCount(f.name) > 0
                      ? '非空收藏集不能删除，请先将其中音乐移到其他收藏集或未分类'
                      : `删除收藏集「${f.name}」（不会删除音乐文件）`
                  "
                  :aria-label="`删除收藏集「${f.name}」`"
                  :disabled="folderBusy || folderCount(f.name) > 0"
                  @click.stop="openDeleteFolderDialog(f.name)"
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </Button>
              </div>
              <p v-if="!sortedFolders.length" class="px-2 py-2 text-xs leading-5 text-muted-foreground">用收藏集组织音乐，不会改变文件路径或 BGM 匹配规则。</p>
            </div>
          </aside>
          <div class="min-w-0 space-y-3">
          <!-- 工具栏 -->
          <div class="flex flex-wrap items-center gap-2">
            <Button size="sm" :disabled="uploading || loading" @click="doUpload()">
              <Loader2 v-if="uploading" class="h-3.5 w-3.5 animate-spin" />
              <Upload v-else class="h-3.5 w-3.5" />
              {{ uploading ? '上传中…' : '批量上传' }}
            </Button>
            <span class="text-xs text-muted-foreground" :title="`上传的文件将永久归属「${uploadTarget || '未分类'}」（也可把 mp3 / wav / flac 直接拖入本卡片）`">
              将保存到：{{ uploadTarget || '未分类' }}
            </span>
            <div class="relative w-48">
              <Search class="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                v-model="search"
                class="h-8 pl-8 text-xs"
                placeholder="搜索文件名…"
              />
            </div>
            <div class="w-44">
              <select
                v-model="tagFilter"
                class="flex h-8 w-full rounded-md border border-input bg-background px-2 py-1 text-xs shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <option value="">全部标签</option>
                <option v-for="o in filterOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
            </div>
            <Button variant="outline" size="sm" :disabled="loading" @click="refreshAll">
              <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />刷新
            </Button>
            <span class="ml-auto text-xs text-muted-foreground">
              <template v-if="view.kind === 'root'">
                共 {{ allTracks.length }} 首 · 启用 {{ enabledCount }}
              </template>
              <template v-else>
                当前视图 {{ trackList.length }} / {{ allTracks.length }} 首 · 启用 {{ enabledCount }}（全库）
              </template>
            </span>
          </div>

          <!-- 文件夹区（仅 root 视图）：全部音乐入口 + 文件夹行 + 排序 + 新建 -->
          <div v-if="false" class="space-y-2">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div class="text-xs font-semibold text-muted-foreground">文件夹</div>
              <div class="flex items-center gap-2">
                <select
                  v-model="folderSort"
                  class="h-7 rounded-md border border-input bg-background px-1.5 text-xs"
                  title="文件夹排序"
                >
                  <option value="name">名称</option>
                  <option value="created_at">创建时间</option>
                  <option value="count">曲目数量</option>
                </select>
                <Button variant="outline" size="sm" :disabled="folderBusy || loading" @click="openCreateFolderDialog">
                  <Plus class="h-3.5 w-3.5" />新建文件夹
                </Button>
              </div>
            </div>

            <button
              type="button"
              class="flex w-full items-center gap-2 rounded-md border bg-card px-3 py-2 text-left transition-colors hover:bg-accent"
              @click="goAll"
            >
              <Music class="h-4 w-4 text-muted-foreground" />
              <span class="text-sm font-medium">全部音乐</span>
              <span class="text-xs text-muted-foreground">{{ allTracks.length }} 首 · 含全部文件夹的曲目</span>
              <ChevronRight class="ml-auto h-4 w-4 text-muted-foreground" />
            </button>

            <div v-if="!sortedFolders.length" class="px-1 text-xs text-muted-foreground">
              还没有文件夹，点击「新建文件夹」创建。
            </div>
            <div
              v-for="f in sortedFolders"
              :key="f.name"
              class="group flex w-full items-center gap-2 rounded-md border bg-card px-3 py-2 text-left transition-colors hover:bg-accent"
              @click="goFolder(f.name)"
            >
              <Folder class="h-4 w-4 text-muted-foreground" />
              <span class="text-sm font-medium">{{ f.name }}</span>
              <span class="text-xs text-muted-foreground">{{ folderCount(f.name) }} 首</span>
              <span v-if="f.created_at" class="text-xs text-muted-foreground">{{ f.created_at.slice(0, 10) }}</span>
              <span
                class="ml-auto flex items-center gap-0.5 opacity-0 focus-within:opacity-100 group-hover:opacity-100"
                @click.stop
              >
                <Button
                  variant="ghost"
                  size="sm"
                  class="h-6 px-1.5"
                  :title="`重命名文件夹「${f.name}」（曲目归属随名称同步更新）`"
                  :disabled="folderBusy"
                  @click="doRenameFolder(f.name)"
                >
                  <Pencil class="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  class="h-6 px-1.5 text-destructive hover:text-destructive"
                  :title="
                    folderCount(f.name) > 0
                      ? '非空文件夹不能删除——请先将其中音乐移到其他文件夹或未分类'
                      : '删除文件夹（仅限空文件夹；不删除任何音乐文件）'
                  "
                  :disabled="folderBusy || folderCount(f.name) > 0"
                  @click="openDeleteFolderDialog(f.name)"
                >
                  <Trash2 class="h-3.5 w-3.5" />
                </Button>
              </span>
            </div>
          </div>

          <!-- 批量操作条（选中 ≥1） -->
          <div
            v-if="selectedNames.length"
            class="flex flex-wrap items-center gap-2 rounded-md border border-primary/30 bg-primary/5 p-2"
          >
            <span class="text-xs font-medium">已选 {{ selectedNames.length }} 首</span>
            <div class="flex items-center gap-1.5">
              <select
                v-model="batchCat"
                class="h-7 rounded-md border border-input bg-background px-1.5 text-xs"
              >
                <option v-for="c in CATEGORIES" :key="c" :value="c">{{ CAT_LABEL[c] }}</option>
              </select>
              <select v-model="batchTag" class="h-7 max-w-40 rounded-md border border-input bg-background px-1.5 text-xs">
                <option value="">选择标签…</option>
                <option v-for="t in lib!.tags[batchCat]" :key="t" :value="t">{{ t }}</option>
              </select>
            </div>
            <Button variant="outline" size="sm" :disabled="batchBusy" @click="doBatchTag('add')">
              <Plus class="h-3.5 w-3.5" />加标签
            </Button>
            <Button variant="outline" size="sm" :disabled="batchBusy" @click="doBatchTag('remove')">
              <X class="h-3.5 w-3.5" />删标签
            </Button>
            <span class="h-4 w-px bg-border" />
            <Button variant="outline" size="sm" :disabled="batchBusy" @click="doBatchEnable(true)">
              启用
            </Button>
            <Button variant="outline" size="sm" :disabled="batchBusy" @click="doBatchEnable(false)">
              禁用
            </Button>
            <span class="h-4 w-px bg-border" />
            <Button variant="destructive" size="sm" :disabled="batchBusy" @click="doBatchDelete">
              <Trash2 class="h-3.5 w-3.5" />批量删除
            </Button>
            <span class="h-4 w-px bg-border" />
            <div class="flex items-center gap-1.5">
              <FolderInput class="h-3.5 w-3.5 text-muted-foreground" />
              <select
                v-model="batchMoveTo"
                class="h-7 max-w-32 rounded-md border border-input bg-background px-1.5 text-xs"
                title="移动目标（全部文件夹 + 未分类）"
              >
                <option v-for="o in folderOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
              </select>
              <Button variant="outline" size="sm" :disabled="batchBusy" @click="doBatchMove">
                移到这里
              </Button>
            </div>
            <span class="h-4 w-px bg-border" />
            <Button variant="outline" size="sm" :disabled="aiBatchBusy" @click="doAiSuggestBatch">
              <Sparkles class="h-3.5 w-3.5" />AI 推荐（{{ selectedNames.length }} 首）
            </Button>
            <Button
              variant="outline"
              size="sm"
              :disabled="aiApplyBusy || !adoptableNames.length"
              :title="adoptableNames.length ? '采用「有 AI 候选 且 未打标签」的选中曲目（不覆盖手动标签）' : '选中曲目里没有可采用的 AI 候选（需有候选且未打任何标签）'"
              @click="doAiApply"
            >
              <BadgeCheck class="h-3.5 w-3.5" />AI 推荐采用（{{ adoptableNames.length }} 首）
            </Button>
            <Button variant="ghost" size="sm" @click="clearSelection">清空选择</Button>
          </div>

          <!-- 行 = 曲目表 -->
          <div class="overflow-x-auto overflow-y-visible rounded-lg border"><Table class="min-w-[66rem]">
            <TableHeader>
              <TableRow>
                <TableHead class="w-8">
                  <input
                    type="checkbox"
                    class="h-4 w-4 accent-primary"
                    :checked="allVisibleSelected"
                    :disabled="!visibleNames.length || uploading"
                    @change="toggleSelectAll"
                  />
                </TableHead>
                <TableHead>文件名</TableHead>
                <TableHead class="w-24">文件夹</TableHead>
                <TableHead class="w-14">格式</TableHead>
                <TableHead class="w-14">时长</TableHead>
                <TableHead class="w-20">大小</TableHead>
                <TableHead class="w-24" title="全部工作空间已保存的章节指派数">章节使用</TableHead>
                <TableHead>标签</TableHead>
                <TableHead class="w-40">AI 识别</TableHead>
                <TableHead class="w-16">启用</TableHead>
                <TableHead class="w-40 text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow v-for="[name, tr] in trackList" :key="name">
                <TableCell>
                  <input
                    type="checkbox"
                    class="h-4 w-4 accent-primary"
                    :checked="!!selected[name]"
                    @change="
                      (e) => {
                        if ((e.target as HTMLInputElement).checked) selected[name] = true
                        else delete selected[name]
                      }
                    "
                  />
                </TableCell>
                <TableCell class="max-w-64">
                  <div class="flex items-center gap-2">
                    <span class="truncate text-sm font-medium" :title="name">{{ name }}</span>
                    <MiniAudioPlayer :src="musicPreviewUrl(name)" />
                  </div>
                </TableCell>
                <TableCell>
                  <Badge
                    v-if="tr.folder"
                    variant="secondary"
                    class="max-w-20 truncate"
                    :title="`所属文件夹：${tr.folder}`"
                  >
                    {{ tr.folder }}
                  </Badge>
                  <Badge v-else variant="secondary" class="opacity-60" title="未归属任何文件夹（根目录上传的曲目）">未分类</Badge>
                </TableCell>
                <TableCell>
                  <Badge variant="secondary" class="font-mono text-[10px] uppercase">{{ extOf(name) || '—' }}</Badge>
                </TableCell>
                <TableCell class="text-xs tabular-nums text-muted-foreground">
                  {{ formatDuration(tr.duration) }}
                </TableCell>
                <TableCell class="text-xs tabular-nums text-muted-foreground">{{ fileSize(tr.size_bytes) }}</TableCell>
                <TableCell class="text-xs tabular-nums text-muted-foreground">
                  {{ tr.use_count == null ? '—' : tr.use_count }}
                </TableCell>
                <TableCell class="w-28">
                  <div
                    v-if="CATEGORIES.some((c) => tr.tags?.[c]?.length)"
                    class="group relative flex h-8 w-24 items-center focus:outline-none"
                    tabindex="0"
                    :aria-label="`查看 ${name} 的全部标签`"
                  >
                    <span
                      v-for="(t, index) in CATEGORIES.flatMap((c) => (tr.tags?.[c] ?? []).map((x) => [c, x])).slice(0, 3)"
                      :key="`${t[0]}-${t[1]}`"
                      class="inline-flex h-7 min-w-7 items-center justify-center rounded-full border px-1 text-[10px] font-bold shadow-sm"
                      :class="[CAT_BADGE[t[0] as MusicTagCategory], index ? '-ml-2.5' : '']"
                      :style="{ zIndex: 4 - index }"
                      :title="String(t[1])"
                    >
                      {{ String(t[1]).slice(0, 1) }}
                    </span>
                    <span
                      v-if="CATEGORIES.flatMap((c) => tr.tags?.[c] ?? []).length > 3"
                      class="ml-1 text-xs font-medium tabular-nums text-muted-foreground"
                    >
                      +{{ CATEGORIES.flatMap((c) => tr.tags?.[c] ?? []).length - 3 }}
                    </span>

                    <div
                      class="pointer-events-none absolute left-0 top-full z-30 mt-2 hidden w-64 rounded-lg border bg-popover p-3 text-popover-foreground shadow-lg group-hover:block group-focus:block"
                    >
                      <p class="mb-2 text-[11px] font-semibold text-muted-foreground">全部标签</p>
                      <div class="flex flex-wrap gap-1.5">
                        <Badge
                          v-for="t in CATEGORIES.flatMap((c) => (tr.tags?.[c] ?? []).map((x) => [c, x]))"
                          :key="`${t[0]}-${t[1]}`"
                          variant="secondary"
                          :class="CAT_BADGE[t[0] as MusicTagCategory]"
                        >
                          {{ t[1] }}
                        </Badge>
                      </div>
                    </div>
                  </div>
                  <Badge v-else variant="secondary" class="whitespace-nowrap opacity-60">未打标</Badge>
                </TableCell>
                <TableCell>
                  <!-- AI 识别行状态：识别中（在途任务）> 识别失败（可重试）> AI 已推荐（候选待确认）> — -->
                  <div
                    v-if="aiTasks.active.has(name)"
                    class="flex items-center gap-1.5 text-xs text-primary"
                  >
                    <Loader2 class="h-3.5 w-3.5 animate-spin" />
                    <span class="truncate" :title="aiTasks.active.get(name)!.current || '识别中…'">
                      {{ aiTasks.active.get(name)!.current || '识别中…' }}
                    </span>
                    <Button variant="ghost" size="sm" class="h-6 px-1.5 text-xs" @click="cancelAiTask(name)">
                      取消
                    </Button>
                  </div>
                  <div v-else-if="aiTasks.failed.get(name)" class="flex items-center gap-1.5">
                    <Badge
                      variant="destructive"
                      class="text-xs"
                      :title="aiTasks.failed.get(name)!.error || 'AI 识别失败'"
                    >
                      识别失败
                    </Badge>
                    <Button variant="ghost" size="sm" class="h-6 px-1.5 text-xs" @click="retryAiTask(name)">
                      重试
                    </Button>
                  </div>
                  <Badge
                    v-else-if="suggestionHasTags(name)"
                    variant="secondary"
                    class="border-amber-500/30 bg-amber-500/15 text-amber-600 text-xs dark:text-amber-400"
                    :title="`AI 候选（未自动采用——曲目已有手动标签）：${suggestionSummary(name)}（「AI 推荐采用」一键采用，或编辑标签确认）`"
                  >
                    <Sparkles class="mr-1 h-3 w-3" />AI 已推荐
                  </Badge>
                  <span
                    v-else-if="suggestionOf(name)"
                    class="text-xs text-muted-foreground"
                  >AI 未推荐到标签</span>
                </TableCell>
                <TableCell>
                  <Switch
                    :model-value="tr.enabled"
                    :class="tr.enabled ? '' : 'opacity-60'"
                    @update:model-value="(v) => toggleEnabled(name, v as boolean)"
                  />
                </TableCell>
                <TableCell>
                  <!-- 移动展开态：目标文件夹 select（全部文件夹 + 未分类）+ 确认/取消 -->
                  <div v-if="moveOpenFor === name" class="flex flex-col items-end gap-1">
                    <select
                      v-model="moveTo"
                      class="h-7 w-32 rounded-md border border-input bg-background px-1.5 text-xs"
                      title="移动目标（全部文件夹 + 未分类）"
                    >
                      <option v-for="o in folderOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
                    </select>
                    <div class="flex items-center gap-1">
                      <Button variant="outline" size="sm" class="h-6 px-1.5" @click="doMoveOne(name)">移动</Button>
                      <Button variant="ghost" size="sm" class="h-6 px-1.5" title="取消" @click="closeMove">
                        <X class="h-3 w-3" />
                      </Button>
                    </div>
                  </div>
                  <div v-else class="flex justify-end gap-1">
                    <Button variant="outline" size="sm" title="编辑标签 / 描述" @click="openEditor(name)">
                      <Pencil class="h-3.5 w-3.5" />编辑
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      class="h-8 w-8 p-0"
                      :title="`移动「${name}」到其他文件夹或未分类`"
                      @click="openMove(name)"
                    >
                      <FolderInput class="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      class="h-8 w-8 p-0 text-destructive hover:text-destructive"
                      title="删除（被锁定引用的曲目会跳过）"
                      @click="doDeleteOne(name)"
                    >
                      <Trash2 class="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </TableCell>
              </TableRow>
            </TableBody>
          </Table></div>

          <p v-if="!trackList.length" class="text-sm text-muted-foreground">
            <template v-if="view.kind === 'folder'">
              「{{ view.name }}」下没有曲目——点「批量上传」，或把 mp3 / wav / flac 直接拖入本卡片（文件将归属该文件夹）。
            </template>
            <template v-else-if="view.kind === 'root'">
              根目录下没有未分类的曲目——点「批量上传」，或把 mp3 / wav / flac 直接拖入本卡片（将归属「未分类」）；进入上方文件夹可查看其中曲目。
            </template>
            <template v-else>当前筛选下没有曲目。</template>
          </p>
          </div>
        </div>
        <div v-else class="space-y-2 py-6 text-center">
          <p class="text-sm text-muted-foreground">音乐库为空——上传 mp3 / wav / flac 开始（也可直接拖入文件）。</p>
          <Button class="mx-auto" :disabled="uploading" @click="doUpload()">
            <Loader2 v-if="uploading" class="h-4 w-4 animate-spin" />
            <Upload v-else class="h-4 w-4" />
            批量上传
          </Button>
        </div>
      </CardContent>
    </Card>

    <!-- 标签管理（注册表） -->
    <Card>
      <CardHeader>
        <CardTitle class="flex items-center gap-2"><Tags class="h-5 w-5" />标签管理</CardTitle>
        <CardDescription>
          管理四类标签词表；修改或删除标签会影响使用它的曲目。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-4">
        <p v-if="tagInfo" class="text-xs text-muted-foreground">
          「{{ CAT_LABEL[tagInfo.cat] }} · {{ tagInfo.name }}」正在被 {{ tagInfo.count }} 首音乐使用。
        </p>
        <div v-for="cat in CATEGORIES" :key="cat" class="space-y-2">
          <div class="text-xs font-semibold text-muted-foreground">{{ CAT_LABEL[cat] }}</div>
          <div class="flex flex-wrap items-center gap-1.5">
            <button
              v-for="t in lib?.tags[cat] ?? []"
              :key="t"
              type="button"
              class="group inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold transition-colors"
              :class="CAT_BADGE[cat]"
              :title="`${CAT_LABEL[cat]} · ${t}（${usageCount(cat, t)} 首音乐使用）`"
              @click="showTagInfo(cat, t)"
            >
              {{ t }}
              <span class="text-[10px] opacity-70">{{ usageCount(cat, t) }}</span>
              <span class="ml-1 hidden gap-0.5 group-hover:inline-flex">
                <span
                  class="cursor-pointer underline opacity-70 hover:opacity-100"
                  @click.stop="doRenameTag(cat, t)"
                >改名</span>
                <span
                  class="cursor-pointer underline opacity-70 hover:opacity-100"
                  @click.stop="doDeleteTag(cat, t)"
                >删除</span>
              </span>
            </button>
            <form
              class="flex items-center gap-1"
              @submit.prevent="doAddTag(cat)"
            >
              <Input
                v-model="newTag[cat]"
                class="h-6 w-24 px-1.5 py-0 text-xs"
                :placeholder="`新增${CAT_LABEL[cat]}标签`"
              />
              <Button variant="outline" size="sm" class="h-6 px-1.5" type="submit">
                <Plus class="h-3 w-3" />
              </Button>
            </form>
          </div>
        </div>
      </CardContent>
    </Card>

    <!-- 新建收藏集弹层 -->
    <div
      v-if="folderDialogOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="presentation"
      @click.self="closeCreateFolderDialog"
    >
      <form
        class="w-full max-w-md rounded-xl border bg-background p-5 shadow-xl"
        aria-labelledby="create-folder-title"
        @submit.prevent="doCreateFolder"
      >
        <div class="flex items-start justify-between gap-4">
          <div>
            <h2 id="create-folder-title" class="flex items-center gap-2 text-base font-semibold">
              <Folder class="h-5 w-5 text-primary" />新建收藏集
            </h2>
            <p class="mt-1 text-xs leading-5 text-muted-foreground">收藏集用于整理音乐，不会改变音频文件路径，也不参与 BGM 标签匹配。</p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            class="h-8 w-8 shrink-0 p-0"
            aria-label="关闭新建收藏集弹窗"
            :disabled="folderBusy"
            @click="closeCreateFolderDialog"
          >
            <X class="h-4 w-4" />
          </Button>
        </div>
        <div class="mt-5 space-y-2">
          <label for="new-folder-name" class="text-sm font-medium">收藏集名称</label>
          <Input
            id="new-folder-name"
            v-model="folderDraft"
            autofocus
            maxlength="50"
            :disabled="folderBusy"
            placeholder="例如：夜晚氛围、战斗音乐"
          />
          <p class="text-xs text-muted-foreground">最多 50 个字符，不支持 `/` 或 `\`。</p>
        </div>
        <div class="mt-6 flex justify-end gap-2">
          <Button type="button" variant="outline" :disabled="folderBusy" @click="closeCreateFolderDialog">取消</Button>
          <Button type="submit" :disabled="folderBusy || !folderDraft.trim()">
            <Loader2 v-if="folderBusy" class="h-4 w-4 animate-spin" />
            创建收藏集
          </Button>
        </div>
      </form>
    </div>

    <!-- 删除收藏集确认弹层 -->
    <div
      v-if="folderDeleteTarget"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="presentation"
      tabindex="-1"
      @click.self="closeDeleteFolderDialog"
      @keydown.esc="closeDeleteFolderDialog"
    >
      <div
        class="w-full max-w-md rounded-xl border bg-background p-5 shadow-xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-folder-title"
        aria-describedby="delete-folder-description"
      >
        <div class="flex items-start gap-3">
          <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-destructive/10 text-destructive">
            <Trash2 class="h-4 w-4" />
          </div>
          <div class="min-w-0">
            <h2 id="delete-folder-title" class="text-base font-semibold">删除收藏集？</h2>
            <p class="mt-1 break-words text-sm font-medium">「{{ folderDeleteTarget }}」</p>
          </div>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            class="ml-auto h-8 w-8 shrink-0 p-0"
            aria-label="关闭删除收藏集弹窗"
            :disabled="folderBusy"
            @click="closeDeleteFolderDialog"
          >
            <X class="h-4 w-4" />
          </Button>
        </div>
        <p id="delete-folder-description" class="mt-4 text-sm leading-6 text-muted-foreground">
          删除后只会移除收藏集，不会删除其中的音乐文件。确定要继续吗？
        </p>
        <div class="mt-6 flex justify-end gap-2">
          <Button type="button" variant="outline" :disabled="folderBusy" @click="closeDeleteFolderDialog">取消</Button>
          <Button type="button" variant="destructive" :disabled="folderBusy" @click="confirmDeleteFolder">
            <Loader2 v-if="folderBusy" class="h-4 w-4 animate-spin" />
            删除收藏集
          </Button>
        </div>
      </div>
    </div>

    <!-- 标签编辑弹层（页内自写 overlay） -->
    <div
      v-if="editor"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      @click.self="closeEditor"
    >
      <div class="max-h-[85vh] w-full max-w-xl space-y-4 overflow-y-auto rounded-lg border bg-background p-5 shadow-lg">
        <div>
          <h2 class="text-base font-semibold">{{ editor.name }}</h2>
          <p class="mt-0.5 text-xs text-muted-foreground">勾选标签并保存；词表外标签自动归入「自定义」。</p>
        </div>

        <div v-for="cat in CATEGORIES" :key="cat" class="space-y-1.5">
          <div class="text-xs font-semibold text-muted-foreground">{{ CAT_LABEL[cat] }}</div>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="t in lib?.tags[cat] ?? []"
              :key="t"
              type="button"
              class="rounded-md border px-2 py-0.5 text-xs font-medium transition-colors"
              :class="editor.tags[cat].includes(t)
                ? CAT_BADGE[cat] + ' ring-1 ring-current'
                : 'border-input text-muted-foreground hover:bg-accent'"
              @click="toggleEditorTag(cat, t)"
            >
              {{ t }}
            </button>
            <span v-if="!(lib?.tags[cat]?.length)" class="text-xs text-muted-foreground">（词表为空）</span>
          </div>
          <div v-if="cat === 'custom'" class="flex items-center gap-1.5">
            <Input
              v-model="customDraft"
              class="h-7 w-40 text-xs"
              placeholder="自定义标签，回车添加"
              @keyup.enter="addCustomTag"
            />
            <Button variant="outline" size="sm" class="h-7" @click="addCustomTag">
              <Plus class="h-3 w-3" />添加
            </Button>
          </div>
        </div>

        <div class="space-y-1.5">
          <div class="text-xs font-semibold text-muted-foreground">描述（AI 推荐用，可空）</div>
          <Textarea v-model="editor.desc" class="min-h-16 text-sm" placeholder="例如：低沉弦乐，适合夜间行路场景" />
        </div>

        <div class="rounded-md border p-3">
          <div class="flex items-center justify-between">
            <span class="text-xs font-semibold">AI 推荐标签</span>
            <Button variant="outline" size="sm" :disabled="aiLoading" @click="doSuggest">
              <Sparkles v-if="!aiLoading" class="h-3.5 w-3.5" />
              <Loader2 v-else class="h-3.5 w-3.5 animate-spin" />
              {{ aiLoading ? '推荐中…' : 'AI 推荐' }}
            </Button>
          </div>
          <p class="mt-1 text-xs text-muted-foreground">
            根据文件名、描述和词表推荐标签，保存后生效。
          </p>
          <template v-if="aiTags">
            <p v-if="aiNote" class="mt-2 text-xs text-primary">{{ aiNote }}</p>
            <div class="mt-2 flex flex-wrap gap-1.5">
              <Badge
                v-for="t in CATEGORIES.flatMap((c) => aiTags![c].map((x) => [c, x]))"
                :key="t[1]"
                variant="secondary"
                :class="CAT_BADGE[t[0] as MusicTagCategory]"
              >
                {{ t[1] }}
              </Badge>
              <span v-if="!CATEGORIES.some((c) => aiTags![c].length)" class="text-xs text-muted-foreground">
                （AI 未推荐到标签）
              </span>
            </div>
            <Button v-if="aiTags" variant="outline" size="sm" class="mt-2" @click="adoptAi">
              全部采用
            </Button>
          </template>
        </div>

        <div class="flex justify-end gap-2">
          <Button variant="outline" @click="closeEditor">关闭</Button>
          <Button :disabled="editorSaving" @click="saveEditor">
            <Loader2 v-if="editorSaving" class="h-4 w-4 animate-spin" />
            确认并修改
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.library-view-item {
  display: flex;
  width: 100%;
  min-width: 0;
  align-items: center;
  gap: 0.5rem;
  border-radius: 0.375rem;
  padding: 0.5rem;
  color: hsl(var(--muted-foreground));
  font-size: 0.8125rem;
  line-height: 1.25rem;
  text-align: left;
  transition: background-color 150ms ease, color 150ms ease;
}

.library-view-item:hover {
  background: hsl(var(--accent));
  color: hsl(var(--accent-foreground));
}

.library-view-item > span:last-child {
  margin-left: auto;
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
  color: hsl(var(--muted-foreground));
}

.library-view-item-active {
  background: hsl(var(--primary) / 0.1);
  color: hsl(var(--primary));
  font-weight: 600;
}
</style>
