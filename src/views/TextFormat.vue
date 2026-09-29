<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSettingsStore } from '@/stores/settings'
import { useToast } from '@/components/ui/toast'
import { useProjectGate } from '@/composables/useProjectGate'
import { useTextFormatWorkbench } from '@/composables/useTextFormatWorkbench'
import { pickFile, type PickedFile } from '@/utils/fileops'
import { formatNumber } from '@/utils/format'
import { stageLabel, chapterBriefLabel, chapterNumWidth, reasonLabel } from '@/utils/bookLabels'
import type { TextToggles } from '@/types'
import type { SplitMode } from '@/api/textFormat'

import Button from '@/components/ui/Button.vue'
import Alert from '@/components/ui/Alert.vue'
import Badge from '@/components/ui/Badge.vue'
import Progress from '@/components/ui/Progress.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { AlertTriangle, ArrowRight, CheckCircle2, FileText, Loader2, RefreshCw, Search, Settings2, X } from 'lucide-vue-next'

import ChapterTable from '@/views/textformat/ChapterTable.vue'
import ChapterDetailPanel from '@/views/textformat/ChapterDetailPanel.vue'
import Pager from '@/views/textformat/Pager.vue'
import FormatSettingsDialog from '@/views/textformat/FormatSettingsDialog.vue'

const router = useRouter()
const settings = useSettingsStore()
const { projectSet } = useProjectGate()
const { push: toast } = useToast()

// Top-level bindings: template refs must be unwrapped at the setup level.
const {
  phase, flow, version, nextTask, activeTasks, loading,
  filteredChapters, pagedChapters, pageCount, page, pageSize,
  filter, query, reasonFilter, sameOrigNum, reasonOptions,
  selectedKey, marksBusy, preview,
  pendingCount, markedCount, settingsDirty, canEnterParse, enterParseReason,
  isMarked, currentChapter, chapterMatters, chapterFile, versionMatters, dupInfo,
  retryFailedStage, toggleMark, selectChapter, startFlow,
} = useTextFormatWorkbench()

// --- file selection --------------------------------------------------------
const sourceFile = ref<PickedFile | null>(null)
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
  // 刷新恢复：文件栏回填流程的源文件（「重新处理」无需重新选择文件）。
  // 源文件已不存在（老数据 adopt 的哨兵 ID 等）时不回填假文件名——那样
  // 「重新处理」只会 404；留空让用户重新选择真实文件即可。
  if (!sourceFile.value?.file_id && f.source_file_id && f.source_file_name) {
    sourceFile.value = { path: '', size: 0, file_id: f.source_file_id, name: f.source_file_name }
  }
})

// --- actions ----------------------------------------------------------------
async function choose() {
  const picked = await pickFile([{ name: '文本文件', extensions: ['txt'] }])
  if (picked) sourceFile.value = picked
}

async function start(restart: boolean) {
  if (!sourceFile.value?.file_id) {
    toast({ title: '请先选择要处理的 TXT 文件', variant: 'destructive' })
    return
  }
  if (!settings.loaded) await settings.load()
  const ok = await startFlow({
    sourceFile: { file_id: sourceFile.value.file_id, name: sourceFile.value.name },
    config: currentConfig.value,
    forceByLength: splitMode.value === 'by_length',
    restart,
  })
  if (ok && restart) {
    toast({ title: '已重新开始处理', variant: 'success', description: '若本次处理失败，可「重试该阶段」或再次重新处理' })
  }
}

// 「选择 TXT」只受 无项目/处理中/有活跃任务/加载 限制——不能要求已选文件，否则永远点不开。
const chooseDisabled = computed(
  () =>
    !projectSet.value ||
    phase.value === 'processing' ||
    activeTasks.value.length > 0 ||
    loading.value,
)
const startDisabled = computed(
  () =>
    !projectSet.value ||
    !sourceFile.value?.file_id ||
    phase.value === 'processing' ||
    activeTasks.value.length > 0 ||
    loading.value,
)
const startLabel = computed(() => (phase.value === 'empty' ? '开始处理' : '重新处理'))

async function onSettingsSave(draft: TextToggles) {
  const ok = await settings.save({ text: draft })
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
  const ok = await settings.save({ text: draft })
  if (!ok) {
    toast({ title: '保存设置失败，请重试', variant: 'destructive', description: '请稍后重试' })
    return
  }
  splitMode.value = mode
  settingsOpen.value = false
  if (!sourceFile.value?.file_id) {
    toast({ title: '请先选择要处理的 TXT 文件', variant: 'destructive' })
    return
  }
  void startFlow({
    sourceFile: { file_id: sourceFile.value.file_id, name: sourceFile.value.name },
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
const chooseLabel = computed(() => (sourceFile.value ? '更换文件' : '选择 TXT'))

// 章节号补齐位数：以最大章节号位数为标准（339 章 → 第001章）
const chapterNumPad = computed(() => chapterNumWidth(version.value?.chapters ?? []))

// 已核对 key 集合：表格「核对状态」徽标随标记实时变化（pending 是后端静态字段）。
const markedKeySet = computed(() => new Set(version.value?.review_marks ?? []))

// 「核对原因」列：key → 简要标签（编号重复类附同原编号章节数）
const chapterBriefs = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  const v = version.value
  if (!v) return map
  const byOrig = new Map<number | null, number>()
  for (const c of v.chapters) {
    if (c.orig_num != null) byOrig.set(c.orig_num, (byOrig.get(c.orig_num) ?? 0) + 1)
  }
  for (const c of v.chapters) {
    if (c.key) map[c.key] = chapterBriefLabel(c.reasons, byOrig.get(c.orig_num) ?? 0)
  }
  return map
})

// --- lifecycle --------------------------------------------------------------------
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
  <div class="wb-page space-y-2">
    <header class="page-header shrink-0">
      <div>
        <p class="eyebrow">Pipeline · Text</p>
        <h1 class="page-title">排版与分册</h1>
        <p class="page-description">整理原文、拆分分册并逐章核对；可离开页面，稍后回来自动继续。</p>
      </div>
    </header>

    <ProjectGateAlert />

    <!-- 文件栏：ready 阶段并入结果摘要行，独立行只在其余阶段展示，省出的高度留给章节列表 -->
    <div v-if="!(phase === 'ready' && version)" class="wb-filebar glass-panel flex flex-wrap items-center gap-3 px-4 py-3">
      <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
        <FileText class="h-5 w-5" />
      </div>
      <div class="min-w-0 flex-1">
        <p class="truncate text-sm font-semibold" :title="sourceFile?.name">
          {{ sourceFile?.name ?? '尚未选择文件' }}
        </p>
        <p class="truncate text-xs text-muted-foreground">
          <template v-if="sourceFile">
            TXT<template v-if="sourceFile.size"> · {{ formatNumber(Math.round(sourceFile.size / 1024)) }} KB</template>
          </template>
          <template v-else>点击右侧「选择 TXT」上传原稿</template>
        </p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <Button variant="outline" size="sm" :disabled="chooseDisabled" @click="choose">
          {{ chooseLabel }}
        </Button>
        <Button variant="outline" size="sm" @click="settingsOpen = true">
          <Settings2 class="h-4 w-4" />
          处理设置
        </Button>
      </div>
    </div>

    <!-- 处理中 -->
    <div v-if="phase === 'processing' && flow" class="glass-panel space-y-3 p-5">
      <div class="flex items-center gap-3">
        <Loader2 class="h-5 w-5 shrink-0 animate-spin" />
        <div class="min-w-0 flex-1">
          <p class="text-sm font-medium">正在{{ stageLabel(nextTask?.stage) }}…</p>
          <p class="text-xs text-muted-foreground">
            排版 → 章节分析 → 分册 自动推进，可离开本页面，稍后回来会继续（不会重复处理）。
          </p>
        </div>
        <span v-if="nextTask" class="shrink-0 text-xs tabular-nums text-muted-foreground">
          {{ Math.round((nextTask.progress ?? 0) * 100) }}%
        </span>
      </div>
      <Progress :value="nextTask?.progress ?? 0" aria-label="流水线进度" />
    </div>

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
    <div v-if="phase === 'ready' && version" class="glass-panel p-4 shrink-0">
      <div class="flex flex-wrap items-center gap-x-8 gap-y-3">
        <!-- 状态块 -->
        <div class="flex shrink-0 items-center gap-3">
          <div class="flex h-9 w-9 items-center justify-center rounded-full bg-emerald-100 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-400">
            <CheckCircle2 class="h-5 w-5" />
          </div>
          <div>
            <p class="text-sm font-semibold text-emerald-700 dark:text-emerald-400">
              {{ version.version_status === 'stale' ? '版本已被覆盖' : '处理完成' }}
            </p>
            <p class="text-xs text-muted-foreground">{{ version.version_status === 'stale' ? '仅可核对，正文不可读' : '已保存到项目' }}</p>
          </div>
        </div>
        <!-- 统计 -->
        <div class="flex flex-wrap items-center gap-y-2">
          <div class="px-6">
            <div class="text-2xl font-bold tabular-nums">{{ formatNumber(version.total_chars) }}</div>
            <div class="text-xs text-muted-foreground">总字数</div>
          </div>
          <div class="border-l px-6">
            <div class="text-2xl font-bold tabular-nums">{{ version.chapters.length }}</div>
            <div class="text-xs text-muted-foreground">最终章节</div>
          </div>
          <div class="border-l px-6">
            <div class="text-2xl font-bold tabular-nums" :class="{ 'text-amber-600 dark:text-amber-400': pendingCount > 0 }">
              {{ pendingCount }}
            </div>
            <div class="text-xs text-muted-foreground">待核对</div>
          </div>
          <div class="border-l px-6">
            <div class="text-2xl font-bold tabular-nums">{{ markedCount }}</div>
            <div class="text-xs text-muted-foreground">已核对</div>
          </div>
        </div>
        <div class="ml-auto flex shrink-0 flex-wrap items-center gap-4 text-xs">
          <Badge v-if="version.version_status === 'stale'" variant="destructive">已被新版本覆盖</Badge>
          <Badge v-if="settingsDirty" variant="warning">设置已修改，重新处理后生效</Badge>
          <!-- 文件信息并入摘要行（原独立文件栏）：加竖向分隔与更宽的间距，避免贴住统计数字显得拥挤 -->
          <div class="h-8 w-px shrink-0 bg-border" aria-hidden="true" />
          <div class="flex min-w-0 items-center gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <FileText class="h-5 w-5" />
            </div>
            <div class="min-w-0">
              <p class="max-w-[22rem] truncate text-sm font-medium" :title="sourceFile?.name">{{ sourceFile?.name ?? '尚未选择文件' }}</p>
              <p class="text-muted-foreground">
                TXT<template v-if="sourceFile?.size"> · {{ formatNumber(Math.round(sourceFile.size / 1024)) }} KB</template>
              </p>
            </div>
          </div>
          <div class="flex shrink-0 items-center gap-3">
            <Button variant="outline" size="sm" :disabled="chooseDisabled" @click="choose">{{ chooseLabel }}</Button>
            <Button variant="outline" size="sm" @click="settingsOpen = true">
              <Settings2 class="h-4 w-4" />
              处理设置
            </Button>
          </div>
        </div>
      </div>
    </div>

    <!-- 版本级事项 -->
    <template v-if="phase === 'ready' && version">
      <Alert v-if="version.version_status === 'stale'" variant="destructive">
        <AlertTriangle class="h-4 w-4 shrink-0" />
        <p>该版本正文已被后续处理覆盖，仅可查看核对记录；正文预览已禁用。</p>
      </Alert>
      <Alert
        v-for="matter in versionMatters"
        :key="matter.id"
        :variant="matter.advisory ? 'warning' : 'info'"
        class="text-xs"
      >
        <AlertTriangle v-if="matter.advisory" class="h-4 w-4 shrink-0" />
        <p>{{ matter.text }}</p>
      </Alert>
    </template>

    <!-- 工作区 -->
    <div v-if="phase === 'ready' && version" class="wb-workspace glass-panel flex min-h-[440px] flex-1 flex-col overflow-hidden">
      <div
        class="min-h-0 flex-1"
        :inert="drawerOpen || undefined"
        @keydown="onKeyNav($event)"
      >
        <!-- 章节结果 -->
        <div class="wb-grid h-full">
          <div class="wb-main flex min-h-0 flex-col">
            <div class="filter-seg flex flex-wrap items-center gap-2 border-b px-4 py-2.5">
              <div class="relative">
                <Search class="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <input
                  v-model="query"
                  type="search"
                  placeholder="搜索章节号或标题"
                  class="h-8 w-56 rounded-md border bg-background pl-8 pr-2 text-sm outline-none focus:ring-2 focus:ring-ring"
                  aria-label="搜索章节"
                />
              </div>
              <button
                v-for="f in [['all', '全部'], ['pending', '待核对'], ['adjusted', '已调整']] as const"
                :key="f[0]"
                type="button"
                class="filter-pill tabular-nums"
                :class="{ 'filter-pill-active': filter === f[0] && sameOrigNum == null }"
                @click="filter = f[0]"
              >
                {{ f[1] }}
                <span class="ml-1">
                  {{ f[0] === 'all' ? version.chapters.length : f[0] === 'pending' ? pendingCount : adjustedCount }}
                </span>
              </button>
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
                class="filter-pill filter-pill-active flex items-center gap-1.5"
                title="点击退出同号章节比较"
                @click="sameOrigNum = null"
              >
                原第{{ sameOrigNum }}章 · {{ sameGroupCount }} 章
                <X class="h-3 w-3" />
              </button>
            </div>
            <div ref="tableScrollEl" class="min-h-0 flex-1 overflow-y-auto">
              <ChapterTable
                :chapters="pagedChapters"
                :selected-key="selectedKey"
                :reason-briefs="chapterBriefs"
                :num-pad="chapterNumPad"
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
              @update:page="(p: number) => (page = p)"
              @update:page-size="(s: number) => { pageSize = s; page = 1 }"
            />
          </div>
          <aside v-if="!isNarrow" class="wb-detail min-h-0 border-l-2 bg-muted/20" aria-label="章节详情">
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
    <div v-if="phase === 'empty'" class="glass-panel p-8 text-center text-sm text-muted-foreground">
      选择 TXT 文件后点击底部「开始处理」：排版 → 章节分析 → 分册 自动完成，随后在此核对章节。
    </div>

    <!-- 底栏 -->
    <div class="bottom-bar glass-panel flex flex-wrap items-center gap-3 px-4 py-3 shrink-0">
      <span v-if="phase === 'ready' && version" class="flex items-center gap-2 text-xs">
        <span class="h-2 w-2 rounded-full bg-emerald-500" />
        <span class="font-medium">结果已保存</span>
        <span class="text-muted-foreground">
          {{ pendingCount > 0 ? `还有 ${pendingCount} 章待核对` : '全部章节已核对' }}
        </span>
      </span>
      <span v-else-if="phase === 'processing'" class="flex items-center gap-2 text-xs">
        <Loader2 class="h-3.5 w-3.5 animate-spin text-primary" />
        <span>处理中，可离开页面，回来自动继续</span>
      </span>
      <span v-else-if="phase === 'failed'" class="flex items-center gap-2 text-xs text-red-600 dark:text-red-400">
        <span class="h-2 w-2 rounded-full bg-red-500" />
        <span>处理失败，请重试或重新处理</span>
      </span>
      <span v-else class="text-xs text-muted-foreground">
        选择 TXT 文件后点击「开始处理」
      </span>
      <div class="ml-auto flex items-center gap-2">
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
      </div>
    </div>

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
/* 页面定高撑满视口（扣除 .app-content 上下 padding）：工作区 flex-1 吃掉底部空隙；
   底栏不贴死窗口下缘，底部留 8px——与 .wb-page space-y-2 的模块间距一致。
   小视口下工作区内部滚动兜底。用 vh 而非 dvh：宿主窗口容器高度等价 100vh，
   dvh 在此环境比容器矮，会残留底部空隙。 */
.wb-page {
  display: flex;
  flex-direction: column;
  /* 抵消 .app-content 的 64px 底 padding，避免底栏下方再叠出一段可滚动留白。 */
  margin-bottom: -64px;
  height: calc(100vh - clamp(28px, 4vw, 52px) - 8px);
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
.filter-pill {
  padding: 0.3rem 0.75rem;
  font-size: 0.75rem;
  color: var(--muted-foreground);
  border: 1px solid var(--border);
  border-radius: 9999px;
  background: var(--background);
}
.filter-pill:hover {
  color: var(--foreground);
  border-color: hsl(var(--primary) / 0.4);
}
.filter-pill-active {
  color: hsl(var(--primary));
  font-weight: 600;
  background: hsl(var(--primary) / 0.1);
  border-color: hsl(var(--primary) / 0.35);
}
/* 桌面：左章节表 + 右常驻详情（40%）：预览正文与核对说明更好读，左侧标题列相应收窄。 */
.wb-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 40%;
  gap: 0;
}
.bottom-bar {
  border-radius: 0.75rem;
}
</style>
