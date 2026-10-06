<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useToast } from '@/components/ui/toast'
import { useProjectGate } from '@/composables/useProjectGate'
import { useWorkbenchScope } from '@/composables/useWorkbenchScope'
import { useTextFormatWorkbench } from '@/composables/useTextFormatWorkbench'
import { pickFiles, type PickedFile } from '@/utils/fileops'
import { uploadFile } from '@/api/files'
import { useProjectStore } from '@/stores/project'
import { useAuthStore } from '@/stores/auth'
import { formatNumber } from '@/utils/format'
import { stageLabel, chapterBriefLabel, chapterNumWidth, reasonLabel } from '@/utils/bookLabels'
import type { TextToggles } from '@/types'
import type { SplitMode } from '@/api/textFormat'

import WorkbenchToolbar from '@/components/WorkbenchToolbar.vue'
import WorkbenchActionBar from '@/components/WorkbenchActionBar.vue'
import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import Button from '@/components/ui/Button.vue'
import Alert from '@/components/ui/Alert.vue'
import WorkbenchStatus from '@/components/ui/WorkbenchStatus.vue'
import Progress from '@/components/ui/Progress.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { ArrowUp, ArrowDown, AlertTriangle, ArrowRight, CheckCircle2, FileText, Loader2, RefreshCw, Settings2, X } from 'lucide-vue-next'

import ChapterTable from '@/views/textformat/ChapterTable.vue'
import ChapterDetailPanel from '@/views/textformat/ChapterDetailPanel.vue'
import Pager from '@/views/textformat/Pager.vue'
import FormatSettingsDialog from '@/views/textformat/FormatSettingsDialog.vue'

const router = useRouter()
const settings = useSettingsStore()
const project = useProjectStore()
const auth = useAuthStore()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()

// Top-level bindings: template refs must be unwrapped at the setup level.
const {
  phase, flow, version, nextTask, activeTasks, loading, pipelineProgress,
  filteredChapters, pagedChapters, pageCount, page, pageSize,
  filter, query, reasonFilter, sameOrigNum, reasonOptions,
  selectedKey, marksBusy, preview,
  pendingCount, markedCount, settingsDirty, canEnterParse, enterParseReason,
  isMarked, currentChapter, chapterMatters, chapterFile, dupInfo,
  retryFailedStage, toggleMark, selectChapter, startFlow, refreshState,
} = useTextFormatWorkbench()

const captureScope = useWorkbenchScope()
const refreshing = ref(false)
async function refreshChapters() {
  if (refreshing.value || !projectSet.value) return
  const isCurrent = captureScope()
  refreshing.value = true
  try {
    const refreshed = await refreshState({ recover: false })
    if (!isCurrent()) return
    if (!refreshed) toast({ title: '刷新章节失败', variant: 'destructive', description: '请稍后重试。' })
  } finally {
    refreshing.value = false
  }
}

// --- file selection --------------------------------------------------------
type SourceDocument = PickedFile & { file_id: string; available?: boolean }
const sourceFiles = ref<SourceDocument[]>([])
const sourcesEdited = ref(false)
const sourcesOpen = ref(true)
const uploading = ref(false)
const sourceFile = computed(() => {
  if (!sourceFiles.value.length) return null
  return {
    name: sourceFiles.value.length === 1 ? sourceFiles.value[0]!.name : `${sourceFiles.value.length} 个源文档合为一本书`,
    size: sourceFiles.value.reduce((sum, file) => sum + file.size, 0),
  }
})
const sourceFormat = computed(() => [...new Set(sourceFiles.value.map((file) => file.name.split('.').pop()?.toUpperCase()))].join(' / '))
const sourcesChanged = computed(() => !!flow.value && JSON.stringify(sourceFiles.value.map((file) => file.file_id)) !== JSON.stringify(flow.value.source_file_ids ?? [flow.value.source_file_id]))
watch([() => project.activeProjectId, () => auth.user?.id], () => {
  sourceFiles.value = []
  sourcesEdited.value = false
  sourcesOpen.value = true
  uploading.value = false
}, { flush: 'sync' })
/** 分册方式：处理设置弹窗的两选一（刷新后回填最近一次运行的选择）。 */
const splitMode = ref<SplitMode>('smart')
const settingsOpen = ref(false)

const DEFAULT_TOGGLES: TextToggles = {
  keep_single_space: false,
  sentence_break: true,
  dialogue_separate: true,
  detect_chapters: true,
  punct_ellipsis: true,
  punct_repeated: true,
  punct_lone_ascii: false,
  punct_quotes: false,
  punct_dash: false,
  live: false,
}
const currentConfig = computed<TextToggles>(() => settings.config?.text ?? DEFAULT_TOGGLES)
const lengthTarget = computed(() => settings.config?.split?.length_target ?? 3000)

// Keep the dialog's 分册方式 choice in sync with the last run's flow.
watch(flow, (f) => {
  if (!f) return
  splitMode.value = f.force_by_length ? 'by_length' : 'smart'
  // Recover ordered inputs; retain explicit local additions/removals until processing.
  if (JSON.stringify(sourceFiles.value.map((file) => file.file_id)) === JSON.stringify(f.source_file_ids ?? [f.source_file_id])) {
    sourcesEdited.value = false
  }
  if (!sourcesEdited.value) {
    if (f.source_files?.length) {
      sourceFiles.value = f.source_files.map((file) => ({ ...file, path: '' }))
    } else if (f.source_file_id && f.source_file_name) {
      sourceFiles.value = [{ path: '', size: 0, file_id: f.source_file_id, name: f.source_file_name }]
    }
  }
})

// --- actions ----------------------------------------------------------------
async function choose() {
  if (chooseDisabled.value) return
  const isCurrent = captureScope()
  const projectId = project.activeProjectId
  uploading.value = true
  try {
    const files = await pickFiles('.txt,.epub')
    if (!isCurrent()) return
    if (sourceFiles.value.length + files.length > 100) {
      toast({ title: '一本书最多添加 100 个源文档', variant: 'destructive' })
      return
    }
    for (const file of files) {
      if (!isCurrent()) return
      if (!/\.(txt|epub)$/i.test(file.name)) {
        toast({ title: '仅支持 TXT 或 EPUB 源文档', description: file.name, variant: 'destructive' })
        continue
      }
      try {
        const uploaded = await uploadFile(file)
        if (!isCurrent() || uploaded.project_id !== projectId) return
        sourceFiles.value.push({ ...uploaded, file_id: uploaded.file_id ?? uploaded.id!, available: true })
        sourcesEdited.value = true
        sourcesOpen.value = true
      } catch (error: any) {
        if (!isCurrent()) return
        toast({ title: `${file.name} 上传失败`, description: error?.message || '请重试', variant: 'destructive' })
      }
    }
  } finally {
    if (isCurrent()) uploading.value = false
  }
}

function moveSource(index: number, delta: number) {
  if (chooseDisabled.value) return
  const target = index + delta
  if (target < 0 || target >= sourceFiles.value.length) return
  const files = [...sourceFiles.value]
  const moved = files.splice(index, 1)[0]!
  files.splice(target, 0, moved)
  sourceFiles.value = files
  sourcesEdited.value = true
}

function removeSource(index: number) {
  if (chooseDisabled.value) return
  sourceFiles.value.splice(index, 1)
  sourcesEdited.value = true
}

async function start(restart: boolean) {
  if (!sourceFiles.value.length || sourceFiles.value.some((file) => file.available === false)) {
    toast({ title: '请先添加可用的 TXT 或 EPUB 源文档', variant: 'destructive' })
    return
  }
  const isCurrent = captureScope()
  if (!settings.loaded) await settings.load()
  if (!isCurrent()) return
  const ok = await startFlow({
    sourceFiles: sourceFiles.value.map((file) => ({ file_id: file.file_id, name: file.name })),
    config: currentConfig.value,
    forceByLength: splitMode.value === 'by_length',
    restart,
  })
  if (ok && isCurrent() && restart) {
    toast({ title: '已重新开始处理', variant: 'success', description: '若本次处理失败，可「重试该阶段」或再次重新处理' })
  }
}

// 「选择文件」只受 无项目/处理中/有活跃任务/加载 限制——不能要求已选文件，否则永远点不开。
const chooseDisabled = computed(
  () =>
    !projectSet.value ||
    uploading.value ||
    phase.value === 'processing' ||
    activeTasks.value.length > 0 ||
    loading.value,
)
const startDisabled = computed(
  () =>
    !projectSet.value ||
    uploading.value ||
    !sourceFiles.value.length ||
    sourceFiles.value.some((file) => file.available === false) ||
    phase.value === 'processing' ||
    activeTasks.value.length > 0 ||
    loading.value,
)
const startLabel = computed(() => (phase.value === 'empty' ? '开始处理' : '重新处理'))

async function onSettingsSave(draft: TextToggles) {
  const isCurrent = captureScope()
  const ok = await settings.save({ text: draft })
  if (!isCurrent()) return
  toast({
    title: ok ? '设置已保存' : '保存失败',
    variant: ok ? 'success' : 'destructive',
    description: ok ? '将在下一次重新处理时生效' : '请稍后重试',
  })
  if (ok) settingsOpen.value = false
}

async function onSettingsReprocess(draft: TextToggles, mode: SplitMode) {
  // 「保存并重新处理」：先落盘设置再按新设置重跑——否则项目配置与流程快照分叉，
  // 摘要行的「设置已修改，重新处理后生效」徽标会常驻。保存失败不启动处理。
  const isCurrent = captureScope()
  const ok = await settings.save({ text: draft })
  if (!isCurrent()) return
  if (!ok) {
    toast({ title: '保存设置失败，请重试', variant: 'destructive', description: '请稍后重试' })
    return
  }
  splitMode.value = mode
  settingsOpen.value = false
  if (!sourceFiles.value.length || sourceFiles.value.some((file) => file.available === false)) {
    toast({ title: '请先添加可用的 TXT 或 EPUB 源文档', variant: 'destructive' })
    return
  }
  void startFlow({
    sourceFiles: sourceFiles.value.map((file) => ({ file_id: file.file_id, name: file.name })),
    config: draft,
    forceByLength: mode === 'by_length',
    restart: true,
  })
}

// --- navigation -----------------------------------------------------------------
function goNext() {
  if (!canEnterParse.value) return
  router.push('/script')
}

// --- selection / drawer (narrow layout) ---------------------------------------
const isNarrow = ref(false)
const drawerOpen = ref(false)
const drawerPanel = ref<HTMLElement | null>(null)
let drawerReturnFocus: HTMLElement | null = null
let mediaMql: MediaQueryList | null = null
const mediaHandler = (e: MediaQueryListEvent) => {
  isNarrow.value = e.matches
  if (!e.matches) drawerOpen.value = false
}

function onChapterSelect(key: string) {
  selectChapter(key)
  if (isNarrow.value) {
    drawerReturnFocus = document.activeElement as HTMLElement | null
    drawerOpen.value = true
  }
}

watch(drawerOpen, (open) => {
  if (open) {
    document.body.style.overflow = 'hidden'
    void nextTick().then(() => {
      drawerPanel.value?.querySelector<HTMLElement>('button:not([disabled])')?.focus()
    })
  } else {
    document.body.style.overflow = ''
    drawerReturnFocus?.focus()
    drawerReturnFocus = null
  }
})

function ensurePageFor(key: string) {
  const idx = filteredChapters.value.findIndex((c) => c.key === key)
  if (idx >= 0) page.value = Math.floor(idx / pageSize.value) + 1
}

// 列表与详情同步定位：选中变化（上/下一章、键盘、核对后自动跳过）时，
// 章节表翻页（既有逻辑）之外再把选中行滚入可视区并高亮，避免左右脱节。
const tableScrollEl = ref<HTMLElement | null>(null)
async function scrollSelectedRow() {
  await nextTick()
  tableScrollEl.value?.querySelector<HTMLElement>('[data-state="selected"]')
    ?.scrollIntoView({ block: 'nearest' })
}
watch(selectedKey, (key) => {
  if (key) void scrollSelectedRow()
})

/** 「查看同号章节」：表格只展示该原编号组便于集中比较，选中行保持在视图内。 */
function onShowSameNumber(origNum: number) {
  sameOrigNum.value = origNum
  if (selectedKey.value) {
    ensurePageFor(selectedKey.value)
    void scrollSelectedRow()
  }
}
const sameGroupCount = computed(() => {
  if (sameOrigNum.value == null) return 0
  return (version.value?.chapters ?? []).filter((c) => c.orig_num === sameOrigNum.value).length
})

function moveSelection(delta: number) {
  const list = filteredChapters.value
  if (!list.length) return
  const idx = list.findIndex((c) => c.key === selectedKey.value)
  const next = list[(idx + delta + list.length) % list.length]
  if (next) {
    ensurePageFor(next.key ?? '')
    selectChapter(next.key ?? null)
  }
}

function onKeyNav(event: KeyboardEvent) {
  if (event.key === 'ArrowUp') {
    event.preventDefault()
    moveSelection(-1)
  } else if (event.key === 'ArrowDown') {
    event.preventDefault()
    moveSelection(1)
  }
}

function onDrawerKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    drawerOpen.value = false
    return
  }
  if (event.key === 'ArrowUp') {
    event.preventDefault()
    moveSelection(-1)
    return
  }
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    moveSelection(1)
    return
  }
  if (event.key !== 'Tab' || !drawerPanel.value) return
  const focusable = [...drawerPanel.value.querySelectorAll<HTMLElement>(
    'button:not([disabled]), input:not([disabled]), select:not([disabled])',
  )]
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

// --- derived display bits -------------------------------------------------------
const detailMatters = computed(() =>
  currentChapter.value ? chapterMatters(currentChapter.value) : [],
)
const detailFileName = computed(() => (currentChapter.value ? chapterFile(currentChapter.value)?.name ?? null : null))

const detailHasPrev = computed(() => {
  const idx = filteredChapters.value.findIndex((c) => c.key === selectedKey.value)
  return idx > 0
})
const detailHasNext = computed(() => {
  const idx = filteredChapters.value.findIndex((c) => c.key === selectedKey.value)
  return idx >= 0 && idx < filteredChapters.value.length - 1
})
const canReadVersion = computed(() => version.value?.version_status === 'current')
const adjustedCount = computed(() => version.value?.chapters.filter((c) => c.adjusted).length ?? 0)
const chooseLabel = computed(() => uploading.value ? '正在上传…' : '添加源文档')

// 章节号补齐位数：以最大章节号位数为标准（339 章 → 第001章）
const chapterNumPad = computed(() => chapterNumWidth(version.value?.chapters ?? []))

// 已核对 key 集合：表格「核对状态」徽标随标记实时变化（pending 是后端静态字段）。
const markedKeySet = computed(() => new Set(version.value?.review_marks ?? []))

// 「核对原因」列：key → 简要标签（编号重复类附同原编号章节数）
const chapterBriefs = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  const v = version.value
  if (!v) return map
  const byOrig = new Map<number | null, Set<string>>()
  for (const c of v.chapters) {
    if (c.orig_num != null) {
      const sources = byOrig.get(c.orig_num) ?? new Set<string>()
      sources.add(c.source_chapter_id ?? c.key ?? String(c.seq))
      byOrig.set(c.orig_num, sources)
    }
  }
  for (const c of v.chapters) {
    if (c.key) map[c.key] = chapterBriefLabel(c.reasons, byOrig.get(c.orig_num)?.size ?? 0)
  }
  return map
})

// --- lifecycle --------------------------------------------------------------------
onDeactivated(() => { uploading.value = false })
onMounted(() => {
  mediaMql = window.matchMedia('(max-width: 1100px)')
  isNarrow.value = mediaMql.matches
  mediaMql.addEventListener('change', mediaHandler)
})
onBeforeUnmount(() => {
  mediaMql?.removeEventListener('change', mediaHandler)
  if (drawerOpen.value) document.body.style.overflow = ''
})
</script>

<template>
  <div class="wb-page viewport-workbench">
    <header class="page-header shrink-0">
      <div>
        <p class="eyebrow">Pipeline · Text</p>
        <h1 class="page-title">排版与分册</h1>
        <p class="page-description">整理原文、拆分分册并逐章核对；多个源文档按顺序合为一本书，可稍后回来继续。</p>
      </div>
    </header>

    <div class="workbench-controls" tabindex="0" role="region" aria-label="制作条件与流程">
      <ProjectGateAlert />

      <!-- 文件栏：ready 阶段并入结果摘要行，独立行只在其余阶段展示，省出的高度留给章节列表 -->
      <WorkbenchContextBar v-if="!(phase === 'ready' && version)">
        <template #icon><FileText /></template>
        <template #title><p :title="sourceFile?.name">{{ sourceFile?.name ?? '尚未选择文件' }}</p></template>
        <template #description>
          <template v-if="sourceFile">{{ sourceFormat }}<template v-if="sourceFile.size"> · {{ formatNumber(Math.round(sourceFile.size / 1024)) }} KB</template></template>
          <template v-else>添加 TXT 或 EPUB 原稿，可多选并按顺序合为一本书</template>
        </template>
        <template #actions>
          <Button variant="outline" size="sm" :disabled="chooseDisabled" @click="choose">{{ chooseLabel }}</Button>
          <Button v-if="sourceFiles.length" variant="ghost" size="sm" :aria-expanded="sourcesOpen" @click="sourcesOpen = !sourcesOpen">{{ sourcesOpen ? '收起源文档' : '管理源文档' }}</Button>
          <Button variant="outline" size="sm" @click="settingsOpen = true"><Settings2 class="h-4 w-4" />处理设置</Button>
        </template>
      </WorkbenchContextBar>

      <!-- 失败 -->
      <Alert v-if="phase === 'failed'" variant="destructive">
        <AlertTriangle class="h-4 w-4 shrink-0" />
        <div>
          <p class="font-medium">流程失败：{{ flow?.error || '未知原因' }}</p>
          <div class="mt-2 flex gap-2">
            <Button size="sm" :disabled="loading" @click="retryFailedStage()">重试该阶段</Button>
            <Button size="sm" variant="outline" :disabled="startDisabled" @click="start(true)">重新处理</Button>
          </div>
        </div>
      </Alert>

      <!-- 结果摘要 -->
      <WorkbenchContextBar v-if="phase === 'ready' && version">
        <template #icon><CheckCircle2 /></template>
        <template #title><p :title="sourceFile?.name">{{ sourceFile?.name ?? '原稿处理结果' }}</p></template>
        <template #description>
          {{ version.version_status === 'stale' ? '版本已被覆盖，仅可核对' : '处理完成，已保存到项目' }}
          <template v-if="sourceFile?.size"> · {{ sourceFormat }} {{ formatNumber(Math.round(sourceFile.size / 1024)) }} KB</template>
          <WorkbenchStatus v-if="settingsDirty" variant="warning"> · 设置已修改，重新处理后生效</WorkbenchStatus>
        </template>
        <template #metrics>
          <div class="workbench-context-metric"><strong>{{ formatNumber(version.total_chars) }}</strong>总字数</div>
          <div class="workbench-context-metric"><strong>{{ version.chapters.length }}</strong>最终章节</div>
          <div class="workbench-context-metric"><strong class="!text-amber-600 dark:!text-amber-400">{{ pendingCount }}</strong>待核对</div>
          <div class="workbench-context-metric"><strong class="!text-emerald-600 dark:!text-emerald-400">{{ markedCount }}</strong>已核对</div>
        </template>
        <template #actions>
          <Button variant="outline" size="sm" :disabled="chooseDisabled" @click="choose">{{ chooseLabel }}</Button>
          <Button v-if="sourceFiles.length" variant="ghost" size="sm" :aria-expanded="sourcesOpen" @click="sourcesOpen = !sourcesOpen">{{ sourcesOpen ? '收起源文档' : '管理源文档' }}</Button>
          <Button variant="outline" size="sm" @click="settingsOpen = true"><Settings2 class="h-4 w-4" />处理设置</Button>
        </template>
      </WorkbenchContextBar>

      <section v-if="sourceFiles.length && sourcesOpen" class="rounded-lg border border-border/60 bg-card/40 p-3" aria-label="源文档顺序">
        <p class="mb-2 text-xs text-muted-foreground">按下列顺序合为一本书，可调整顺序或移除文档。移除仅改变本次列表。</p>
        <ol class="max-h-36 overflow-y-auto">
          <li v-for="(file, index) in sourceFiles" :key="file.file_id" class="flex items-center gap-2 py-1 text-sm">
            <span class="w-6 shrink-0 text-muted-foreground">{{ index + 1 }}</span>
            <span class="min-w-0 flex-1 truncate" :title="file.name">{{ file.name }}</span>
            <span v-if="file.available === false" class="text-xs text-destructive">源文件已删除</span>
            <Button variant="ghost" size="sm" :disabled="chooseDisabled || index === 0" :aria-label="`上移 ${file.name}`" @click="moveSource(index, -1)"><ArrowUp class="h-4 w-4" /></Button>
            <Button variant="ghost" size="sm" :disabled="chooseDisabled || index === sourceFiles.length - 1" :aria-label="`下移 ${file.name}`" @click="moveSource(index, 1)"><ArrowDown class="h-4 w-4" /></Button>
            <Button variant="ghost" size="sm" :disabled="chooseDisabled" :aria-label="`移除 ${file.name}`" @click="removeSource(index)"><X class="h-4 w-4" /></Button>
          </li>
        </ol>
      </section>
      <p v-if="sourcesChanged" class="text-xs text-muted-foreground">源文档列表已修改，点击「重新处理」后应用。</p>

      <!-- 版本级事项 -->
      <template v-if="phase === 'ready' && version">
        <Alert v-if="version.version_status === 'stale'" variant="destructive">
          <AlertTriangle class="h-4 w-4 shrink-0" />
          <p>该版本正文已被后续处理覆盖，仅可查看核对记录；正文预览已禁用。</p>
        </Alert>
        <!-- 版本级处理提示（matters）不再在页顶铺全宽 banner：每章的处置说明已由
             章节表「核对原因」列与详情面板章节级 matter 卡承载，页顶只留版本状态警示。 -->
      </template>
    </div>

    <!-- 处理状态使用同一标准工作区，避免底栏随进度卡片上移。 -->
    <div v-if="phase === 'processing' && flow" class="wb-workspace workbench-empty glass-panel" role="status" aria-live="polite">
      <div class="w-full max-w-xl space-y-4 text-left">
        <div class="flex items-center gap-3">
          <Loader2 class="h-5 w-5 shrink-0 animate-spin text-primary" />
          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium">正在{{ stageLabel(nextTask?.stage) }}…</p>
            <p class="mt-1 text-xs text-muted-foreground">排版 → 章节分析 → 分册，可离开页面，回来自动继续。</p>
          </div>
          <span v-if="nextTask" class="shrink-0 text-xs tabular-nums text-muted-foreground">{{ Math.floor(pipelineProgress * 100) }}%</span>
        </div>
        <Progress :value="pipelineProgress" aria-label="流水线总进度" />
      </div>
    </div>
    <!-- 工作区 -->
    <div v-if="phase === 'ready' && version" class="wb-workspace glass-panel flex min-h-0 flex-1 flex-col overflow-hidden">
      <div
        class="min-h-0 flex-1"
        :inert="drawerOpen || undefined"
        @keydown="onKeyNav($event)"
      >
        <!-- 章节结果 -->
        <div class="wb-grid h-full">
          <div class="wb-main flex min-h-0 flex-col">
            <WorkbenchToolbar
              v-model:query="query"
              :filter="sameOrigNum == null ? filter : ''"
              @update:filter="filter = $event as typeof filter"
              placeholder="搜索章节号或标题"
              :filters="[{ key: 'all', label: '全部', count: version.chapters.length }, { key: 'pending', label: '待核对', count: pendingCount }, { key: 'adjusted', label: '已调整', count: adjustedCount }]"
              :loading="loading || refreshing"
              :refresh-disabled="!projectSet"
              @refresh="refreshChapters"
            >
              <template #filters>
                <select
                  v-model="reasonFilter"
                  class="h-8 rounded-md border bg-background px-1.5 text-xs"
                  aria-label="按核对原因筛选"
                >
                  <option value="">全部原因</option>
                  <option v-for="r in reasonOptions" :key="r" :value="r">{{ reasonLabel(r) }}</option>
                </select>
                <button
                  v-if="sameOrigNum != null"
                  type="button"
                  class="flex h-8 items-center gap-1.5 rounded-full bg-primary/10 px-3 text-xs text-primary"
                  title="点击退出同号章节比较"
                  @click="sameOrigNum = null"
                >
                  原第{{ sameOrigNum }}章 · {{ sameGroupCount }} 章
                  <X class="h-3 w-3" />
                </button>
              </template>
            </WorkbenchToolbar>
            <div ref="tableScrollEl" class="min-h-0 flex-1 overflow-y-auto">
              <ChapterTable
                :chapters="pagedChapters"
                :selected-key="selectedKey"
                :reason-briefs="chapterBriefs"
                :num-pad="chapterNumPad"
                :page-size="pageSize"
                :marked-keys="markedKeySet"
                @select="onChapterSelect"
              />
            </div>
            <Pager
              class="border-t px-4 py-2"
              :page="page"
              :page-count="pageCount"
              :total="filteredChapters.length"
              :page-size="pageSize"
              :page-size-options="[10, 20, 50]"
              @update:page="(p: number) => (page = p)"
              @update:page-size="(s: number) => { pageSize = s; page = 1 }"
            />
          </div>
          <aside v-if="!isNarrow" class="wb-detail min-h-0 border-l bg-muted/20" aria-label="章节详情">
            <ChapterDetailPanel
              :chapter="currentChapter"
              :matters="detailMatters"
              :file-name="detailFileName"
              :preview="preview"
              :marked="isMarked(selectedKey)"
              :marks-busy="!!marksBusy"
              :can-read="canReadVersion"
              :num-pad="chapterNumPad"
              :dup-info="dupInfo"
              :has-prev="detailHasPrev"
              :has-next="detailHasNext"
              @prev="moveSelection(-1)"
              @next="moveSelection(1)"
              @mark="() => toggleMark(selectedKey)"
              @show-same-number="onShowSameNumber"
            />
          </aside>
        </div>
      </div>
    </div>

    <!-- 空态提示 -->
    <div v-if="phase === 'empty' || phase === 'failed'" class="wb-workspace workbench-empty glass-panel text-sm text-muted-foreground">
      {{ phase === 'failed' ? '处理未完成，请重试该阶段或重新处理。' : '添加 TXT 或 EPUB 源文档后点击底部「开始处理」：排版 → 章节分析 → 分册 自动完成，随后在此核对章节。' }}
    </div>

    <!-- 底栏 -->
    <WorkbenchActionBar>
      <template #summary>
        <div v-if="phase === 'ready' && version" class="text-xs">
          <span class="mr-2 inline-block h-2 w-2 rounded-full bg-emerald-500" />
          <span class="font-medium">结果已保存</span>
          <span class="mt-1 block text-muted-foreground">
            {{ pendingCount > 0 ? `还有 ${pendingCount} 章待核对` : '全部章节已核对' }}
          </span>
        </div>
        <span v-else-if="phase === 'processing'" class="flex items-center gap-2 text-xs">
          <Loader2 class="h-3.5 w-3.5 animate-spin text-primary" />
          <span>处理中，可离开页面，回来自动继续</span>
        </span>
        <span v-else-if="phase === 'failed'" class="flex items-center gap-2 text-xs text-red-600 dark:text-red-400">
          <span class="h-2 w-2 rounded-full bg-red-500" />
          <span>处理失败，请重试或重新处理</span>
        </span>
        <span v-else class="text-xs text-muted-foreground">
          添加 TXT 或 EPUB 源文档后点击「开始处理」
        </span>
      </template>
      <Button
        size="sm"
        :variant="phase === 'empty' ? 'default' : 'outline'"
        :disabled="startDisabled"
        @click="start(phase !== 'empty')"
      >
        <Loader2 v-if="phase === 'processing'" class="h-4 w-4 animate-spin" />
        <RefreshCw v-else class="h-4 w-4" />
        {{ phase === 'processing' ? '处理中…' : startLabel }}
      </Button>
      <Button size="sm" :disabled="!canEnterParse" :title="enterParseReason" @click="goNext">
        进入文本解析
        <ArrowRight class="h-4 w-4" />
      </Button>
    </WorkbenchActionBar>

    <!-- 窄屏详情抽屉 -->
    <Teleport to="body">
      <div v-if="drawerOpen && isNarrow" class="fixed inset-0 z-40">
        <div class="absolute inset-0 bg-black/40" @click="drawerOpen = false" />
        <section
          ref="drawerPanel"
          role="dialog"
          aria-modal="true"
          aria-label="章节详情"
          class="absolute right-0 top-0 flex h-full w-[85vw] max-w-[380px] flex-col border-l bg-background shadow-xl"
          @keydown="onDrawerKeydown"
        >
          <header class="flex items-center justify-between border-b px-4 py-3">
            <span class="text-sm font-semibold">章节详情</span>
            <Button variant="ghost" size="icon" class="h-7 w-7" aria-label="关闭详情" @click="drawerOpen = false">
              <X class="h-4 w-4" />
            </Button>
          </header>
          <div class="min-h-0 flex-1">
            <ChapterDetailPanel
              :chapter="currentChapter"
              :matters="detailMatters"
              :file-name="detailFileName"
              :preview="preview"
              :marked="isMarked(selectedKey)"
              :marks-busy="!!marksBusy"
              :can-read="canReadVersion"
              :num-pad="chapterNumPad"
              :dup-info="dupInfo"
              :has-prev="detailHasPrev"
              :has-next="detailHasNext"
              @prev="moveSelection(-1)"
              @next="moveSelection(1)"
              @mark="() => toggleMark(selectedKey)"
              @show-same-number="onShowSameNumber"
            />
          </div>
        </section>
      </div>
    </Teleport>

    <FormatSettingsDialog
      :open="settingsOpen"
      :initial="settings.config?.text ?? null"
      :length-target="lengthTarget"
      :busy="phase === 'processing' || activeTasks.length > 0"
      :split-mode="splitMode"
      @close="settingsOpen = false"
      @save="onSettingsSave"
      @reprocess="onSettingsReprocess"
    />
  </div>
</template>

<style scoped>
/* 高度由外层视口提供，工作区只占剩余空间。 */
.wb-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}
/* 标题区底部留白从全局 28px 收紧：摘要行与章节列表整体上移，多留可视行数（同 .bgm-page 先例）。 */
.wb-page .page-header {
  margin-bottom: 8px;
}
.wb-filebar {
  border-radius: 0.75rem;
  flex-shrink: 0;
}
.wb-workspace {
  border-radius: 0.75rem;
}
.wb-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 34%;
  gap: 0;
}
</style>
