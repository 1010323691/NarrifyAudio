<script setup lang="ts">
import { vFitRows } from '@/directives/fitRows'
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { useListPage } from '@/composables/useListPage'
import Pager from '@/views/textformat/Pager.vue'
import { useSettingsStore } from '@/stores/settings'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import { batchList, listVoices } from '@/api/tts'
import { bgmPreviewUrl } from '@/api/bgm'
import * as preview from '@/api/chapterPreview'
import type { ChapterPreviewDetail, VoiceItem } from '@/types'
import {
  buildEdits,
  isDirty,
  lineStatus,
  restoreDraft,
  saveState,
  type LineTriple,
  type PreviewLineStatus,
} from '@/utils/previewLineState'
import { speakerColor } from '@/utils/speakerColor'
import {
  TONE_TAG_CATEGORIES,
  findToneConflicts,
  selectedTagInCategory,
  textHasTag,
  toggleTag,
} from '@/utils/toneTags'
import { useLabelDerivedTasks, labelKeyOf } from '@/composables/useLabelDerivedTasks'
import {
  AlertTriangle, Check, Clock3, Eye, FileText, Loader2, Play, RefreshCw, Save, Search, Tag, Trash2, Undo2, X, XCircle,
} from 'lucide-vue-next'

import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import Button from '@/components/ui/Button.vue'
import WorkbenchStatus from '@/components/ui/WorkbenchStatus.vue'
import Card from '@/components/ui/Card.vue'
import Alert from '@/components/ui/Alert.vue'
import MiniAudioPlayer from '@/components/ui/MiniAudioPlayer.vue'
import AudioPlayButton from '@/components/ui/AudioPlayButton.vue'
import BadgeAudioButton from '@/components/ui/BadgeAudioButton.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useProjectGate } from '@/composables/useProjectGate'

const settings = useSettingsStore()
const taskStore = useTaskStore()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()

// ---------------------------------------------------------------------------
// 左栏：章节列表（懒加载——点章节才拉详情；口径 = 03_parsed_json 基础 *.json，
// 排除 _checked 孤儿，与后端 resolve_parsed_json_all 一致）
// ---------------------------------------------------------------------------

interface ChapterRow {
  name: string
  title: string
  total: number
  completed: number
  complete: boolean
  merged: boolean
  mixed: boolean
}

const chapterRows = ref<ChapterRow[]>([])
const listLoading = ref(false)
const listError = ref('')
const query = ref('')

function chapterTitle(name: string): string {
  const stem = name.replace(/\.json$/, '')
  const m = stem.match(/第\s*[0-9一二三四五六七八九十百千零两]+\s*章(?:\s+[一-龥A-Za-z0-9]+)?/)
  return m ? m[0].replace(/\s+/g, ' ') : stem
}

const page = ref(1)
const pageSize = ref(10)
function changePageSize(size: number) { pageSize.value = size; if (page.value !== 1) page.value = 1; else void refreshList() }
const listPage = useListPage(refreshList, () => { chapterRows.value = []; detail.value = null; openName.value = ''; voices.value = []; drafts.value = {} })
const pagination = listPage.pagination
watch(query, () => { page.value = 1; void refreshList() })
watch(page, () => { void refreshList() })

async function refreshList() {
  listLoading.value = true
  listError.value = ''
  const signal = listPage.begin()
  try {
    const response = await batchList({ page: page.value, page_size: pageSize.value, q: query.value, filter: 'all' }, signal)
    if (signal.aborted) return
    listError.value = ''
    listPage.received(response.pagination, page)
    chapterRows.value = response.files.filter(row => row.is_script !== false).map(row => ({
      name: row.name, title: row.display_name || chapterTitle(row.name), total: row.total, completed: row.completed,
      complete: row.complete, merged: !!row.merged, mixed: !!row.mixed,
    }))
  } catch (e: any) {
    if (!signal.aborted) listError.value = e?.message || '刷新失败'
  } finally {
    if (!signal.aborted) listLoading.value = false
  }
}

const visibleRows = computed(() => chapterRows.value)

// ---------------------------------------------------------------------------
// 详情（右栏内嵌，点章节才加载；左栏章节列表常驻）
// ---------------------------------------------------------------------------

const openName = ref('')
const detail = ref<ChapterPreviewDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const voices = ref<VoiceItem[]>([])
const drafts = ref<Record<number, LineTriple>>({})
const selected = ref<number | null>(null)
const saving = ref(false)
const rerenderBusy = ref<number | null>(null)
const downstreamDirty = ref(false)
const purgeRunning = ref(false)

/** 常用语气标签浮窗状态：fixed 定位依触发按钮 rect，向上空间不足则翻转向下。 */
const tagPopup = ref<{
  catKey: string
  rect: { top: number; bottom: number; left: number }
  openBelow: boolean
  triggerEl: HTMLElement | null
} | null>(null)

/** label 尾部「：{script}·第N句」→ 行任务 key（与后端 label 契约一致）。 */
const lineKey = (index: number) => `${openName.value}·第${index + 1}句`
const previewTasks = useLabelDerivedTasks('preview')

/** 打开章节 = 详情请求 + voices；F5 恢复：staged.ok 且 ≠ 磁盘行的句子用 staged 还原草稿
 *  （重开页面后直接 previewReady，已渲染的编辑不丢）。 */
async function openChapter(name: string) {
  if (openName.value && gate.value.dirtyCount > 0) {
    const ok = await showConfirm(
      '当前章节有未保存的修改（含已生成未保存的句子），切换将丢弃这些修改。',
      { title: '切换章节', destructive: true },
    )
    if (!ok) return
  }
  openName.value = name
  selected.value = null
  detail.value = null
  detailError.value = ''
  tagPopup.value = null
  downstreamDirty.value = false
  drafts.value = {}
  await loadDetail({ resetDrafts: true })
}

/** 拉详情；resetDrafts=true 时按 staged/磁盘重建草稿（打开/保存成功后），否则保留编辑中草稿。 */
async function loadDetail(opts?: { resetDrafts?: boolean; silent?: boolean }) {
  if (!openName.value) return
  if (!opts?.silent) detailLoading.value = true
  try {
    const [d, v] = await Promise.all([
      preview.previewChapter(openName.value),
      listVoices(openName.value).catch(() => null),
    ])
    detail.value = d
    voices.value = v?.speakers ?? []
    if (opts?.resetDrafts || Object.keys(drafts.value).length === 0) {
      drafts.value = {}
      for (const line of d.lines) {
        drafts.value[line.index] = restoreDraft(line.staged, {
          text: line.text, speaker: line.speaker, instruct: line.instruct,
        })
      }
    }
  } catch (e: any) {
    detailError.value = e?.message || '加载失败'
  } finally {
    detailLoading.value = false
  }
}

const gate = computed(() => {
  const base = saveState(lineViews.value.map((v) => ({
    disk: v.disk, draft: v.draft, staged: v.staged, rendering: v.rendering,
  })))
  // 服务端 /apply 的在途检查更严格：本章任一 tts.preview_render 在途（哪怕该句已恢复原句）
  // 也会 409——前端按同口径提前禁用，避免白等一次 409。
  const inflight = [...previewTasks.value.active.keys()].some((k) => k.startsWith(`${openName.value}·`))
  return { ...base, ready: base.ready && !inflight }
})

interface ViewLine {
  index: number
  disk: LineTriple
  draft: LineTriple
  status: PreviewLineStatus
  staged: ChapterPreviewDetail['lines'][number]['staged']
  rendering: boolean
  renderFailed: boolean
  line: ChapterPreviewDetail['lines'][number]
}

const lineViews = computed<ViewLine[]>(() => {
  if (!detail.value) return []
  const { active, failed } = previewTasks.value
  return detail.value.lines.map((line) => {
    const disk: LineTriple = { text: line.text, speaker: line.speaker, instruct: line.instruct }
    const draft = drafts.value[line.index] ?? disk
    const key = lineKey(line.index)
    const rendering = active.has(key)
    const renderFailed = failed.has(key)
    return {
      index: line.index,
      disk,
      draft,
      status: lineStatus({ disk, draft, staged: line.staged, rendering, renderFailed }),
      staged: line.staged,
      rendering,
      renderFailed,
      line,
    }
  })
})

type BadgeVariant = 'default' | 'secondary' | 'success' | 'warning' | 'destructive' | 'outline'

/** 行状态 → 展示徽标（纯展示派生，不改变行状态机本身；normal 再按有无正式音频细分）。
 *  对外统一「生成」措辞（不再说「渲染 / 重渲染」）。 */
function rowBadge(v: ViewLine): { label: string; variant: BadgeVariant } {
  switch (v.status) {
    case 'rendering':
      return { label: '生成中', variant: 'secondary' }
    case 'failed':
      return { label: '生成失败', variant: 'destructive' }
    case 'previewReady':
      return { label: '预览就绪', variant: 'success' }
    case 'dirty':
      return { label: '待生成', variant: 'warning' }
    default:
      return v.line.ok ? { label: '可试听', variant: 'outline' } : { label: '未合成', variant: 'secondary' }
  }
}

/** 选中句（编辑器目标；展示派生）。 */
const selectedView = computed<ViewLine | null>(() =>
  selected.value === null ? null : lineViews.value.find((v) => v.index === selected.value) ?? null,
)

/** 当前章节在左栏列表中的行（header 状态条展示）。 */
const openRow = computed(() => chapterRows.value.find((r) => r.name === openName.value) ?? null)

/** 试听来源标签：预览音频（主色）/ 正式音频 / 章节合并音频（不定位）。 */
function auditionSourceLabel(v: ViewLine): string {
  if (v.status === 'previewReady' && v.staged?.file) return '预览音频'
  if (v.line.ok && v.line.audio) return '正式音频'
  return '章节合并音频'
}

function updateDraft(index: number, field: keyof LineTriple, value: string) {
  const line = lineViews.value.find((v) => v.index === index)
  const cur = drafts.value[index] ?? line?.disk ?? { text: '', speaker: '', instruct: '' }
  drafts.value[index] = { ...cur, [field]: value }
}

/** 选中行试听源：previewReady → 暂存新音频（?v=rendered_at）；否则正式 05；都没有 → 章节级。 */
function auditionSrc(v: ViewLine): string | null {
  const d = detail.value
  if (!d) return null
  if (v.status === 'previewReady' && v.staged?.file) {
    return preview.previewAudioUrl(`${d.package}/${v.staged.file}`, v.staged.rendered_at)
  }
  if (v.line.ok && v.line.audio) return preview.formalAudioUrl(v.line.audio, v.line.audio_mtime_ns)
  if (d.chapter_audio?.path) return preview.formalAudioUrl(d.chapter_audio.path, null)
  return null
}

function isChapterFallback(v: ViewLine): boolean {
  return v.status !== 'previewReady' && !v.line.ok && !!detail.value?.chapter_audio?.path
}

/** 行上显示的时长（秒）：仅当试听源是正式 05 单句音频时。
 * previewReady（试听新暂存音频）→ null，避免把旧正式时长说成本句时长；
 * 章节回退（无单句音频）→ null，章节总时长已由顶部「已合并」徽标展示。 */
function rowDuration(v: ViewLine): number | null {
  if (v.status === 'previewReady' && v.staged?.file) return null
  return v.line.ok && v.line.audio ? (v.line.duration ?? null) : null
}

/** 试听源已知时长（秒）：章节合并回退用整章时长（播放源=整章）；否则用正式单句时长（Inspector 播放前不再 0:00）。 */
function auditionKnownDuration(v: ViewLine): number | null {
  if (isChapterFallback(v)) return detail.value?.chapter_audio?.duration ?? null
  return rowDuration(v)
}

/** 当前选中句在章节音频中的近似起点（秒）——章节试听从选中句开播；未选中句 / 无起点时 null（从头播）。 */
const selectedStartOffset = computed<number | null>(() => {
  if (selected.value == null) return null
  const line = detail.value?.lines.find((l) => l.index === selected.value)
  return line?.start_offset ?? null
})

/** 章节级音频时长展示（ffprobe 秒数 → 「X 分 Y 秒」/「Y 秒」）。 */
function fmtDuration(sec: number | null): string {
  if (sec === null || !Number.isFinite(sec)) return ''
  if (sec < 60) return `${Math.round(sec)} 秒`
  const m = Math.floor(sec / 60)
  return `${m} 分 ${Math.round(sec % 60)} 秒`
}

// ---------------------------------------------------------------------------
// 常用语气标签建议：6 分类气泡浮窗，点标签把词**拼入**（不覆盖）当前句 instruct。
// chip 选中态与点击行为全部以 instruct 文本为唯一真相（textHasTag/selectedTagInCategory）：
// 命中 → 移除自身；未命中 → 末尾追加，故手动增删文本 / 手写标签词都能实时反映到 chip。
// ---------------------------------------------------------------------------

const TAG_POPUP_W = 224
const TAG_POPUP_H = 168 // 估算浮窗高（约 2–3 行标签 + 标题 + 内边距），据此决定上/下展开

function openTagCat(catKey: string, el: HTMLElement): void {
  if (tagPopup.value?.catKey === catKey) {
    closeTagPopup()
    return
  }
  const r = el.getBoundingClientRect()
  tagPopup.value = {
    catKey,
    rect: { top: r.top, bottom: r.bottom, left: r.left },
    openBelow: r.top < TAG_POPUP_H + 8, // 上方空间不足 → 翻转向下，避免被右栏顶部/视口顶部裁切
    triggerEl: el,
  }
}

/** 关闭浮窗并把焦点归还触发按钮（点外部 / Esc / 选完标签）。 */
function closeTagPopup(): void {
  tagPopup.value?.triggerEl?.focus?.()
  tagPopup.value = null
}

function pickTag(tag: string): void {
  const v = selectedView.value
  if (!v) return
  // 文本为唯一真相：整词命中则移除、未命中则末尾追加（只增删自身，不碰同类其他词，含手写）。
  updateDraft(v.index, 'instruct', toggleTag(v.draft.instruct, tag))
  closeTagPopup()
}

/** 冲突提示（非阻断、措辞留余地）：全文扫描到相反要求词对时提示，用户可自行忽略。 */
function conflictHint(text: string): string {
  const pairs = findToneConflicts(text)
  if (!pairs.length) return ''
  const shown = pairs.slice(0, 2).map((p) => `「${p.a}」与「${p.b}」`).join('、')
  return `提示：${shown}${pairs.length > 2 ? ' 等' : ''}可能存在相反的要求，请确认（若为「先…后…」/否定式描述可忽略）。`
}

/** 浮窗 fixed 定位（依视口，逃逸右栏滚动容器裁切）：水平钳制在视口内，默认向上、空间不足翻转向下。 */
const tagPopupStyle = computed(() => {
  const p = tagPopup.value
  if (!p) return { left: '', top: '', width: '' }
  let left = p.rect.left
  const width = Math.min(TAG_POPUP_W, window.innerWidth - 16)
  const maxLeft = window.innerWidth - width - 8
  left = Math.max(8, Math.min(left, maxLeft))
  let top = p.openBelow ? p.rect.bottom + 6 : Math.max(8, p.rect.top - TAG_POPUP_H - 6)
  const phone = window.matchMedia('(max-width: 600px), (max-width: 880px) and (max-height: 500px) and (pointer: coarse)').matches
  if (phone) {
    const height = window.visualViewport?.height ?? window.innerHeight
    const popupHeight = Math.min(240, height - 16)
    top = Math.max(8, Math.min(p.openBelow ? p.rect.bottom + 6 : p.rect.top - popupHeight - 6, height - popupHeight - 8))
  }
  return { left: `${left}px`, top: `${top}px`, width: `${width}px` }
})

/** 浮窗当前展开的分类对象（null = 浮窗关闭）——供模板直接取 label/tags，避免模板内 .find 闭包丢失 v-if 收窄。 */
const popupCategory = computed(() => {
  const p = tagPopup.value
  if (!p) return null
  return TONE_TAG_CATEGORIES.find((c) => c.key === p.catKey) ?? null
})

// 切句即收起浮窗（provenance 按句保留，不受影响）。
watch(() => selected.value, () => closeTagPopup())

/** 角色下拉：voices 数据源（未就绪角色禁用），当前磁盘角色若不在 voices 里也保留为选项。 */
function speakerOptions(): { name: string; ready: boolean }[] {
  const names = new Set(voices.value.map((s) => s.name))
  const out: { name: string; ready: boolean }[] = []
  for (const v of lineViews.value) {
    const name = v.disk.speaker
    if (name && !names.has(name)) {
      names.add(name)
      out.push({ name, ready: false })
    }
  }
  for (const s of voices.value) out.push({ name: s.name, ready: s.status === 'ready' })
  return out
}

/** 草稿角色声音是否就绪：在 voices 列表且未就绪 → 前端拦截（避免必然失败的重渲染，
 *  如「不支持的声音类型」）；不在列表中不拦截，由后端 TTS 做最终裁决。 */
function draftSpeakerReady(v: ViewLine): boolean {
  const voice = voices.value.find((s) => s.name === v.draft.speaker.trim())
  return !voice || voice.status === 'ready'
}

function canRerender(v: ViewLine): boolean {
  return (
    projectSet.value
    && !saving.value
    && v.status !== 'normal'
    && !v.rendering
    && rerenderBusy.value === null
    && draftSpeakerReady(v)
  )
}

/** 重新渲染本句：只提交被改字段（partial triple）；409（在途冲突）由服务端裁决。 */
async function rerender(v: ViewLine) {
  if (!canRerender(v)) return
  rerenderBusy.value = v.index
  try {
    const opts: { script: string; index: number; text?: string; speaker?: string; instruct?: string } = {
      script: openName.value,
      index: v.index,
    }
    if (v.draft.text.trim() !== v.disk.text.trim()) opts.text = v.draft.text.trim()
    if (v.draft.speaker.trim() !== v.disk.speaker.trim()) opts.speaker = v.draft.speaker.trim()
    if (v.draft.instruct.trim() !== v.disk.instruct.trim()) opts.instruct = v.draft.instruct.trim()
    await preview.rerenderPreviewLine(opts)
    await taskStore.refresh()
  } catch (e: any) {
    toast({ title: '生成提交失败', variant: 'destructive', description: e?.message || '未知错误' })
  } finally {
    rerenderBusy.value = null
  }
}

function restoreOriginal(v: ViewLine) {
  if (!isDirty(v.draft, v.disk)) return
  drafts.value[v.index] = { ...v.disk }
}

/** 后端 500 的 detail 是 dict（ok/stage/error/…/message）；client.ts 会把 dict detail
 *  JSON.stringify 成错误消息 → e.message 是 JSON 字符串。这里从中取出面向用户的 message 字段；
 *  解析失败（如 409 的 detail 本就是纯字符串）则原样返回。 */
function saveErrorMessage(e: any): string {
  const raw: string = typeof e?.message === 'string' ? e.message : ''
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && typeof parsed.message === 'string') return parsed.message
  } catch {
    /* not JSON */
  }
  return raw
}

/** 保存修改：服务端是唯一权威门禁（staged==有效三元组 + 章节锁 + 备份回滚）。 */
async function doSave() {
  if (!gate.value.ready || !detail.value || saving.value) return
  saving.value = true
  try {
    const edits = buildEdits(lineViews.value.map((v) => ({ index: v.index, disk: v.disk, draft: v.draft })))
    const res = await preview.applyPreviewEdits(openName.value, edits)
    const artifactNames: Record<string, string> = { merged: '章节合并音频', mixed: '混音音频', timeline: '段落时间轴' }
    const invalid = res.invalidated.map((k) => artifactNames[k] ?? k).join('、')
    toast({
      title: '保存成功',
      variant: 'success',
      description: `${res.edited.length} 句已更新` + (invalid ? `；已使失效：${invalid}` : ''),
    })
    downstreamDirty.value = res.downstream_dirty
    if (res.downstream_dirty) {
      toast({
        title: '旧产物删除失败',
        variant: 'destructive',
        description: '旧产物不可再视为有效，可点「重试清理」。',
      })
    }
    await loadDetail({ resetDrafts: true })
    await refreshList()
  } catch (e: any) {
    toast({ title: '保存失败', variant: 'destructive', description: saveErrorMessage(e) || '未知错误' })
    // 500 = 已字节级回滚，磁盘与保存前一致 → 静默重同步详情（草稿保留）
    await loadDetail({ silent: true })
  } finally {
    saving.value = false
  }
}

/** 重试清理旧产物（downstream_dirty 的补救入口）。 */
async function retryPurge() {
  if (!openName.value || purgeRunning.value) return
  purgeRunning.value = true
  try {
    const res = await preview.purgePreviewStale(openName.value)
    if (!res.downstream_dirty) {
      downstreamDirty.value = false
      toast({
        title: '旧产物清理完成',
        variant: 'success',
        description: res.invalidated.length ? `已删除：${res.invalidated.join('、')}` : '',
      })
      await loadDetail({ silent: true })
      await refreshList()
    } else {
      toast({
        title: '清理仍有失败项',
        variant: 'destructive',
        description: res.failures.map((f) => `${f.artifact}：${f.error}`).join('；'),
      })
    }
  } catch (e: any) {
    toast({ title: '清理失败', variant: 'destructive', description: e?.message || '未知错误' })
  } finally {
    purgeRunning.value = false
  }
}

/** 关闭/切换章节（有未保存修改 → 确认丢弃；Esc / X 按钮 / 底部「取消」同路径）。 */
async function close(force = false) {
  if (!force && gate.value.dirtyCount > 0) {
    const ok = await showConfirm(
      '有未保存的修改（含已生成未保存的句子），关闭将丢弃这些修改。',
      { title: '关闭确认', destructive: true },
    )
    if (!ok) return
  }
  openName.value = ''
  detail.value = null
  detailError.value = ''
  drafts.value = {}
  selected.value = null
  downstreamDirty.value = false
  tagPopup.value = null
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (tagPopup.value) {
    closeTagPopup()
    return
  }
  if (openName.value) void close()
}

// ---------------------------------------------------------------------------
// SSE 驱动：本章重渲染任务终态 → 静默重拉详情（拿最新 staged，前端重新推导 previewReady）。
// 页面不取消任务——任务属项目，后台继续。
// ---------------------------------------------------------------------------

watch(
  () => taskStore.projectTasks.filter((t) => t.module === 'preview').map((t) => `${t.id}:${t.status}`).join('|'),
  () => {
    if (!openName.value || !detail.value) return
    const prefix = `${openName.value}·`
    for (const t of taskStore.projectTasks) {
      if (t.module !== 'preview') continue
      if (!labelKeyOf(t.label).startsWith(prefix)) continue
      if (['succeeded', 'failed', 'cancelled'].includes(t.status)) {
        void loadDetail({ silent: true })
        return
      }
    }
  },
)

// keep-alive 缓存页：重新进入时刷新左栏（磁盘口径）。
// Escape 监听挂在 window 级，必须随 keep-alive 的激活态摘除/恢复——否则切走页面后
// 监听器常驻，在其他页按 Esc 会误触发本页 close()（可能弹「丢弃未保存修改」确认）。
onMounted(() => {
  if (!settings.loaded) void settings.load()
  void refreshList()
})
onActivated(() => {
  if (settings.loaded) void refreshList()
  window.addEventListener('keydown', onKeydown)
})
onDeactivated(() => {
  window.removeEventListener('keydown', onKeydown)
})
// 兜底：非 keep-alive 路径（切项目/退出登录触发 unmount）确保监听被摘除（幂等）。
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <div class="preview-page flex min-h-0 flex-1 flex-col gap-3">
    <header class="page-header shrink-0">
      <p class="eyebrow">Pipeline · Preview</p>
      <h1 class="page-title">整章预览</h1>
      <p class="page-description">逐句试听、修改角色与语气并单句重新生成；修改后可先试听，保存后才会更新本章。</p>
    </header>

    <WorkbenchContextBar class="shrink-0" role="region" aria-label="章节预览功能区">
      <template #icon><FileText /></template>
      <template #title><p :title="openName || undefined">{{ openName ? chapterTitle(openName) : '选择章节开始预览' }}</p></template>
      <template #description>
        <span v-if="openRow" :title="`共 ${openRow.total} 句中，${openRow.completed} 句拥有与当前声音配置一致的音频`">当前章节 · 已合成 {{ openRow.completed }}/{{ openRow.total }}</span>
        <span v-else>选章节 → 选台词 → 修改与试听 → 保存</span>
      </template>
      <template #actions>
        <BadgeAudioButton
          v-if="openName && detail?.chapter_audio?.path"
          :src="preview.formalAudioUrl(detail.chapter_audio.path, null)"
          label="已合并"
          tone="teal"
          :known-duration="detail.chapter_audio.duration"
          :start-at="selectedStartOffset"
        />
        <BadgeAudioButton
          v-if="openName && detail?.downstream?.mixed"
          :src="bgmPreviewUrl(openName.replace(/\.json$/, ''))"
          label="已混音"
          tone="sky"
          :start-at="selectedStartOffset"
        />
        <Button v-if="openName" variant="ghost" size="icon" class="h-8 w-8" title="刷新详情" aria-label="刷新详情" :disabled="detailLoading" @click="loadDetail()">
          <RefreshCw class="h-3.5 w-3.5" :class="detailLoading ? 'animate-spin' : ''" />
        </Button>
        <Button v-else variant="ghost" size="icon" class="h-8 w-8" title="刷新章节列表" aria-label="刷新章节列表" :disabled="listLoading || !projectSet" @click="refreshList">
          <RefreshCw class="h-3.5 w-3.5" :class="listLoading ? 'animate-spin' : ''" />
        </Button>
        <Button v-if="openName" variant="ghost" size="icon" class="h-8 w-8" title="关闭章节" aria-label="关闭章节" @click="close()">
          <X class="h-3.5 w-3.5" />
        </Button>
      </template>
    </WorkbenchContextBar>

    <ProjectGateAlert />

    <!-- 主三栏：章节列表 → 台词列表 → 台词编辑 -->
    <div class="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[280px_minmax(0,1fr)_360px] 2xl:grid-cols-[300px_minmax(0,1fr)_380px]">
      <!-- 左栏：章节列表（常驻；独立纵向滚动，禁横向滚动） -->
      <Card class="flex min-h-0 flex-col overflow-hidden">
        <div class="shrink-0 border-b border-border/70 px-3 py-2.5">
          <div class="flex items-center justify-between gap-2">
            <h2 class="text-xs font-bold tracking-wide">
              章节<span class="ml-1.5 font-medium text-muted-foreground">共 {{ pagination?.total ?? chapterRows.length }} 章</span>
            </h2>
            <Button v-if="openName" variant="ghost" size="icon" class="h-6 w-6" title="刷新章节列表" :disabled="listLoading" @click="refreshList">
              <RefreshCw class="h-3 w-3" :class="listLoading ? 'animate-spin' : ''" />
            </Button>
          </div>
          <div class="relative mt-2">
            <Search class="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              v-model="query"
              type="text"
              placeholder="搜索章节…"
              class="h-8 w-full rounded-md border border-input bg-background/60 pl-7 pr-2 text-xs outline-none focus:ring-1 focus:ring-ring"
            />
          </div>
        </div>

        <Alert v-if="listError" variant="destructive" class="m-2 shrink-0">
          <template #icon><AlertTriangle class="h-4 w-4 shrink-0" /></template>
          {{ listError }}
        </Alert>

        <div class="fixed-list p-1.5" :class="{ 'is-scroll': pageSize > 10 }" style="--fl-pad: 12px" v-fit-rows="{ prop: '--fl-row', max: 56, min: 50 }">
          <div
            v-for="row in visibleRows"
            :key="row.name"
            class="fl-row relative flex flex-col justify-center cursor-pointer rounded-lg py-1 pl-3 pr-2 transition-colors"
            :class="row.name === openName ? 'bg-accent/80' : 'hover:bg-accent/40'"
            @click="openChapter(row.name)"
          >
            <span v-if="row.name === openName" class="absolute bottom-1.5 left-0 top-1.5 w-[3px] rounded-full bg-primary" />
            <div class="flex items-center gap-2">
              <span class="min-w-0 flex-1 truncate text-[13px] font-semibold" :title="row.name">{{ row.title }}</span>
              <span
                class="shrink-0 text-[11px] font-semibold tabular-nums"
                :class="row.complete ? 'text-emerald-600 dark:text-emerald-400' : 'text-muted-foreground'"
                :title="`共 ${row.total} 句中，${row.completed} 句拥有与当前声音配置一致的音频`"
              >
                {{ row.completed }}/{{ row.total }}
              </span>
            </div>
            <div class="mt-1.5 flex items-center gap-1.5">
              <div class="h-1 w-14 shrink-0 overflow-hidden rounded-full bg-muted">
                <div class="h-full rounded-full bg-primary" :style="{ width: `${row.total ? (row.completed / row.total) * 100 : 0}%` }" />
              </div>
              <span v-if="row.merged" class="shrink-0 text-[10px] font-semibold text-teal-600 dark:text-teal-400">已合并</span>
              <span v-if="row.mixed" class="shrink-0 text-[10px] font-semibold text-sky-600 dark:text-sky-400">已混音</span>
              <span v-if="!row.complete && row.total > 0" class="shrink-0 text-[10px] font-medium text-muted-foreground">
                {{ row.completed === 0 ? '未合成' : `${row.total - row.completed} 句待合成` }}
              </span>
            </div>
          </div>
          <p v-if="!visibleRows.length" class="px-2 py-6 text-center text-xs text-muted-foreground">
            {{ listLoading ? '加载中…' : '暂无已解析章节，请先到「文本解析」生成剧本。' }}
          </p>
        </div>
        <Pager :page="page" :page-count="Math.max(1, Math.ceil((pagination?.total ?? 0) / pageSize))" :total="pagination?.total ?? 0" :page-size="pageSize" unit="章" @update:page="page = $event" @update:page-size="changePageSize" />
      </Card>

      <!-- 中栏：台词列表（视觉中心） -->
      <Card v-if="openName" class="flex min-h-0 flex-col overflow-hidden">
        <div class="flex shrink-0 items-center justify-between gap-3 border-b border-border/70 px-3.5 py-2">
          <h2 class="text-xs font-bold tracking-wide">
            台词<span class="ml-1.5 font-medium text-muted-foreground">{{ detail?.lines.length ?? 0 }} 句 · 点击行进入编辑</span>
          </h2>
          <span v-if="selectedView" class="truncate text-[11px] tabular-nums text-muted-foreground" :title="selectedView.draft.text">
            选中第 {{ selectedView.index + 1 }} 句
          </span>
        </div>

        <!-- 台词列表：章节级试听（06 合并 / 08 混音）不放这里，改在头部状态条的
             「已合并 / 已混音」徽标上直接播放（播放中撑开进度条），列表区留给逐句内容。 -->
        <Alert v-if="detailError" variant="destructive" class="m-2 shrink-0">
          <template #icon><AlertTriangle class="h-4 w-4 shrink-0" /></template>
          {{ detailError }}
        </Alert>

        <div v-if="detail" class="min-h-0 flex-1 space-y-1 overflow-y-auto p-1.5">
          <div
            v-for="v in lineViews"
            :key="v.index"
            class="relative cursor-pointer rounded-lg py-2 pl-3 pr-3 transition-colors"
            :class="selected === v.index ? 'bg-accent/80 ring-1 ring-inset ring-primary/35' : 'hover:bg-accent/35'"
            @click="selected = v.index"
          >
            <span v-if="selected === v.index" class="absolute bottom-1.5 left-0 top-1.5 w-[3px] rounded-full bg-primary" />
            <div class="flex items-center gap-2.5">
              <span class="w-6 shrink-0 text-right text-[11px] font-bold tabular-nums text-muted-foreground">{{ v.index + 1 }}</span>
              <span class="shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold" :style="speakerColor(v.disk.speaker)">
                {{ v.disk.speaker || '（无角色）' }}
              </span>
              <span
                class="min-w-0 flex-1 text-[13px] leading-relaxed"
                :class="{ 'line-clamp-2': selected !== v.index }"
                :title="v.draft.text"
              >{{ v.draft.text || '（空台词）' }}</span>
              <WorkbenchStatus :variant="rowBadge(v).variant" class="shrink-0">
                <Loader2 v-if="v.status === 'rendering'" class="h-3 w-3 animate-spin" />
                <Check v-else-if="v.status === 'previewReady'" class="h-3 w-3" />
                <XCircle v-else-if="v.status === 'failed'" class="h-3 w-3" />
                <Clock3 v-else-if="v.status === 'dirty'" class="h-3 w-3" />
                <Play v-else-if="v.line.ok" class="h-3 w-3" />
                {{ rowBadge(v).label }}
              </WorkbenchStatus>
            </div>
            <div class="mt-1.5 flex items-center gap-2 pl-[30px]">
              <span v-if="v.line.instruct" class="min-w-0 max-w-[14rem] truncate text-[11px] italic text-muted-foreground" :title="v.line.instruct">
                「{{ v.line.instruct }}」
              </span>
              <span v-else class="shrink-0 text-[11px] italic text-muted-foreground/50">默认语气</span>
              <div class="ml-auto flex shrink-0 items-center gap-2">
                <span v-if="v.status === 'failed' && v.staged?.reason" class="max-w-[16rem] truncate text-[11px] text-destructive" :title="v.staged.reason">
                  {{ v.staged.reason }}
                </span>
                <template v-if="auditionSrc(v)">
                  <span v-if="v.status === 'previewReady'" class="text-[10px] font-semibold text-primary">预览</span>
                  <span v-if="rowDuration(v) != null" class="text-[11px] tabular-nums text-muted-foreground">{{ fmtDuration(rowDuration(v)) }}</span>
                  <AudioPlayButton :src="auditionSrc(v)!" />
                </template>
              </div>
            </div>
            <p v-if="isChapterFallback(v)" class="mt-1 pl-[30px] text-[11px] text-muted-foreground">
              本句暂无独立音频，播放章节合并音频（不定位到具体台词）
            </p>
          </div>
        </div>
        <div v-else-if="!detailError" class="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
          <Loader2 class="mr-2 h-4 w-4 animate-spin" />加载章节中…
        </div>
      </Card>
      <div v-else class="hidden min-h-0 lg:block">
        <Card class="flex h-full min-h-[20rem] items-center justify-center">
          <div class="flex flex-col items-center gap-2.5 px-8 text-center">
            <Eye class="h-8 w-8 text-muted-foreground/50" />
            <p class="text-sm font-medium text-muted-foreground">选择左侧章节开始整章预览</p>
            <p class="text-xs text-muted-foreground/80">选章节 → 选台词 → 修改与试听 → 保存</p>
          </div>
        </Card>
      </div>

      <!-- 右栏：台词编辑（Inspector） -->
      <Card v-if="openName" class="flex min-h-0 flex-col overflow-hidden">
        <div class="flex shrink-0 items-center justify-between border-b border-border/70 px-3.5 py-2">
          <h2 class="text-xs font-bold tracking-wide">台词编辑</h2>
          <span v-if="selectedView" class="text-[11px] tabular-nums text-muted-foreground">当前：第 {{ selectedView.index + 1 }} 句</span>
        </div>
        <div class="min-h-0 flex-1 overflow-y-auto">
          <div v-if="selectedView" class="space-y-3.5 p-3.5">
            <!-- 角色 -->
            <label class="block">
              <span class="mb-1 block text-[11px] font-semibold text-muted-foreground">角色</span>
              <select
                class="h-9 w-full rounded-md border border-input bg-background/60 px-2 text-sm outline-none focus:ring-1 focus:ring-ring"
                :value="selectedView.draft.speaker"
                @change="updateDraft(selectedView.index, 'speaker', ($event.target as HTMLSelectElement).value)"
              >
                <option
                  v-for="opt in speakerOptions()"
                  :key="opt.name"
                  :value="opt.name"
                  :disabled="!opt.ready && opt.name !== selectedView.draft.speaker"
                >{{ opt.name }}{{ opt.ready ? '' : '（声音未就绪）' }}</option>
              </select>
            </label>
            <p
              v-if="!draftSpeakerReady(selectedView)"
              class="flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-[11px] leading-snug text-amber-700 dark:text-amber-400"
            >
              <AlertTriangle class="h-3 w-3 shrink-0" />
              <span>角色「{{ selectedView.draft.speaker }}」声音未就绪，暂不能生成本句。</span>
              <RouterLink to="/voices" class="shrink-0 font-semibold underline underline-offset-2">到角色配音配置 →</RouterLink>
            </p>

            <!-- 台词 -->
            <div>
              <div class="mb-1 flex items-baseline justify-between">
                <span class="text-[11px] font-semibold text-muted-foreground">台词</span>
                <span class="text-[11px] tabular-nums text-muted-foreground">{{ selectedView.draft.text.length }} 字</span>
              </div>
              <textarea
                rows="5"
                class="w-full resize-y rounded-md border border-input bg-background/60 px-2.5 py-2 text-sm leading-relaxed outline-none focus:ring-1 focus:ring-ring"
                :value="selectedView.draft.text"
                @input="updateDraft(selectedView.index, 'text', ($event.target as HTMLTextAreaElement).value)"
              ></textarea>
            </div>

            <!-- 语气 / 指令（多行换行：长英文指令完整可见、便于检查与编辑） -->
            <div>
              <div class="mb-1 text-[11px] font-semibold text-muted-foreground">语气 / 指令</div>
              <textarea
                rows="2"
                class="w-full resize-y rounded-md border border-input bg-background/60 px-2.5 py-1.5 text-sm leading-relaxed outline-none focus:ring-1 focus:ring-ring"
                :value="selectedView.draft.instruct"
                placeholder="如：平静地、压低声音"
                @input="updateDraft(selectedView.index, 'instruct', ($event.target as HTMLTextAreaElement).value)"
              ></textarea>

              <!-- 冲突提示（全文扫描、非阻断、措辞留余地——用户可自行忽略否定/先后描述） -->
              <p
                v-if="conflictHint(selectedView.draft.instruct)"
                class="mt-1.5 flex items-start gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-[11px] leading-snug text-amber-700 dark:text-amber-400"
              >
                <AlertTriangle class="mt-0.5 h-3 w-3 shrink-0" />
                <span>{{ conflictHint(selectedView.draft.instruct) }}</span>
              </p>

              <!-- 常用语气标签（点击 = 追加到指令，拼入一个标签；每分类至多一个，替换只动面板自己插的词） -->
              <div class="mt-2">
                <div class="mb-1 text-[10px] text-muted-foreground/80">常用语气标签（点击追加到指令）</div>
                <div class="flex flex-wrap gap-1.5">
                  <button
                    v-for="cat in TONE_TAG_CATEGORIES"
                    :key="cat.key"
                    type="button"
                    class="inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    :class="tagPopup?.catKey === cat.key
                      ? 'border-primary bg-primary/10 text-primary'
                      : selectedTagInCategory(selectedView.draft.instruct, cat.key)
                        ? 'border-primary/50 bg-primary/5 text-primary'
                        : 'border-border/70 bg-background/60 text-muted-foreground hover:border-primary/40 hover:text-primary'"
                    :title="selectedTagInCategory(selectedView.draft.instruct, cat.key)
                      ? `已选：${selectedTagInCategory(selectedView.draft.instruct, cat.key)}（点击修改或取消）`
                      : `选择${cat.label}标签`"
                    @click="openTagCat(cat.key, $event.currentTarget as HTMLElement)"
                  >
                    <Tag class="h-3 w-3 shrink-0" />
                    <span>{{ cat.label }}</span>
                  </button>
                </div>
              </div>
            </div>

            <!-- 常用语气标签浮窗：Teleport 到 <body> 逃逸右栏 glass-panel 的 backdrop-filter
                 （backdrop-filter 会把祖先变成 fixed 的包含块，导致 fixed 相对面板定位而跑出视口）。
                 fixed 依视口定位 + 上/下翻转 + 焦点归还 + Esc 关闭。 -->
            <Teleport to="body">
              <div v-if="tagPopup" class="fixed inset-0 z-40" role="presentation" @click="closeTagPopup"></div>
              <div
                v-if="tagPopup"
                class="preview-tag-popup fixed z-50 rounded-lg border border-border/70 bg-popover p-2 text-popover-foreground shadow-lg"
                :style="tagPopupStyle"
                role="dialog"
                :aria-label="popupCategory ? `${popupCategory.label}标签` : ''"
                @keydown.esc="closeTagPopup"
              >
                <div class="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold">
                  <Tag class="h-3 w-3" />
                  {{ popupCategory?.label }}
                </div>
                <div class="flex flex-wrap gap-1">
                  <button
                    v-for="tag in (popupCategory?.tags ?? [])"
                    :key="tag"
                    type="button"
                    class="rounded border px-1.5 py-0.5 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    :class="textHasTag(selectedView.draft.instruct, tag)
                      ? 'border-primary bg-primary/10 text-primary'
                      : 'border-border/70 bg-background/60 text-muted-foreground hover:border-primary/40 hover:text-primary'"
                    :title="tag"
                    @click="pickTag(tag)"
                  >{{ tag }}</button>
                </div>
              </div>
            </Teleport>

            <hr class="border-border/60" />

            <!-- 操作（生成本句）：唯一主按钮 + 弱化次按钮 -->
            <div class="flex gap-2">
              <Button class="flex-1" size="sm" :disabled="!canRerender(selectedView)" @click="rerender(selectedView)">
                <Loader2 v-if="selectedView.rendering" class="h-3.5 w-3.5 animate-spin" />
                <RefreshCw v-else class="h-3.5 w-3.5" />生成本句
              </Button>
              <Button variant="outline" size="sm" :disabled="!isDirty(selectedView.draft, selectedView.disk)" @click="restoreOriginal(selectedView)">
                <Undo2 class="h-3.5 w-3.5" />撤销本句修改
              </Button>
            </div>

            <hr class="border-border/60" />

            <!-- 试听（本句）：来源标记（正式 / 预览 / 章节合并）+ 完整播放器 + 生成结果块 -->
            <div>
              <div class="mb-1.5 flex items-center justify-between">
                <span class="text-[11px] font-semibold text-muted-foreground">试听</span>
                <span
                  v-if="auditionSrc(selectedView)"
                  class="text-[11px] font-semibold"
                  :class="selectedView.status === 'previewReady' && selectedView.staged?.file ? 'text-primary' : 'text-muted-foreground'"
                >{{ auditionSourceLabel(selectedView) }}</span>
              </div>
              <div class="rounded-lg border border-border/70 bg-background/50 p-2.5">
                <MiniAudioPlayer v-if="auditionSrc(selectedView)" :src="auditionSrc(selectedView)!" :known-duration="auditionKnownDuration(selectedView)" preload-metadata />
                <p v-else class="text-xs text-muted-foreground">暂无可播放音频。</p>
                <p v-if="isChapterFallback(selectedView)" class="mt-1.5 text-[11px] leading-snug text-muted-foreground">
                  本句暂无独立音频，当前播放章节合并音频，不定位到具体台词。
                </p>
              </div>

              <div
                v-if="selectedView.status === 'previewReady'"
                class="mt-2 flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-2"
              >
                <Check class="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                <div class="min-w-0 text-xs">
                  <p class="font-semibold text-emerald-700 dark:text-emerald-300">预览音频已生成</p>
                  <p class="text-[11px] tabular-nums text-muted-foreground">{{ selectedView.staged?.rendered_at }}</p>
                </div>
              </div>
              <p v-else-if="selectedView.status === 'failed'" class="mt-2 flex items-start gap-2 text-xs text-destructive">
                <XCircle class="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>生成失败：{{ selectedView.staged?.reason || '原因未知' }}</span>
              </p>
            </div>
          </div>
          <div v-else class="flex min-h-[10rem] items-center justify-center text-sm text-muted-foreground">
            点击左侧任意句子开始编辑。
          </div>
        </div>
      </Card>
      <div v-else class="hidden min-h-0 lg:block" />
    </div>

    <!-- 底部 Action Bar（开章节时显示）：下游失效 / 门禁状态在左，操作在右 -->
    <footer
      v-if="openName"
      class="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-border bg-card/85 px-4 py-2.5 shadow-sm backdrop-blur-sm"
    >
      <div v-if="downstreamDirty" class="flex items-center gap-2 text-xs text-destructive">
        <AlertTriangle class="h-4 w-4 shrink-0" />
        <span class="font-medium">保存成功，但旧产物删除失败，不可再视为有效。</span>
        <Button variant="outline" size="sm" class="h-7 px-2 text-[11px]" :disabled="purgeRunning" @click="retryPurge">
          <Trash2 class="h-3 w-3" :class="purgeRunning ? 'animate-spin' : ''" />重试清理
        </Button>
      </div>
      <!-- downstream_dirty 优先级最高、独占左侧；否则：下游产物失效提示只在「确有未保存修改」时出现
           （无修改时旧产物仍然有效，不该用警告色），修改计数常驻。 -->
      <template v-else>
        <div
          v-if="gate.dirtyCount > 0 && (detail?.downstream.merged || detail?.downstream.mixed || detail?.downstream.timeline)"
          class="flex flex-wrap items-center gap-2 text-xs"
        >
          <AlertTriangle class="h-4 w-4 shrink-0 text-amber-500" />
          <span class="text-amber-700 dark:text-amber-400">保存后，本章以下产物将失效，需要重新生成：</span>
          <span v-if="detail?.downstream.merged" class="rounded-md border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-400">章节合并音频</span>
          <span v-if="detail?.downstream.mixed" class="rounded-md border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-400">混音音频</span>
          <span v-if="detail?.downstream.timeline" class="rounded-md border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-400">段落时间轴</span>
        </div>
        <div class="text-xs text-muted-foreground">
          {{ gate.dirtyCount ? `已修改 ${gate.dirtyCount} 句，待保存` : '暂无修改' }}
        </div>
      </template>

      <div class="ml-auto flex items-center gap-3">
        <span v-if="!gate.ready && gate.dirtyCount > 0" class="text-xs font-semibold text-amber-600 dark:text-amber-400">
          还有 {{ gate.pending }} 句尚未完成生成
        </span>
        <Button variant="outline" size="sm" :disabled="saving" @click="close()">{{ gate.dirtyCount ? '放弃本章修改' : '关闭预览' }}</Button>
        <Button size="sm" :disabled="!projectSet || saving || !gate.ready" @click="doSave">
          <Loader2 v-if="saving" class="h-3.5 w-3.5 animate-spin" />
          <Save v-else class="h-3.5 w-3.5" />保存修改
        </Button>
      </div>
    </footer>
  </div>
</template>

<style scoped>
/* 工作台高度：全宽容器（.app-content--full）占满视口，页面自身撑满其高度 → .app-main 不产生页面级滚动。 */
.preview-page {
  height: 100%;
}
.preview-page .page-header {
  margin-bottom: 0;
}
</style>
