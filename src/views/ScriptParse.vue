<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectGate } from '@/composables/useProjectGate'
import { useScriptParseWorkbench } from '@/composables/useScriptParseWorkbench'

import WorkbenchToolbar from '@/components/WorkbenchToolbar.vue'
import BatchSubmissionRecovery from '@/components/BatchSubmissionRecovery.vue'
import { useProjectStore } from '@/stores/project'
import WorkbenchActionBar from '@/components/WorkbenchActionBar.vue'
import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import Button from '@/components/ui/Button.vue'
import Alert from '@/components/ui/Alert.vue'
import WorkbenchStatus from '@/components/ui/WorkbenchStatus.vue'
import ProjectGateAlert from '@/components/ui/ProjectGateAlert.vue'
import { useClientDisplayStore } from '@/stores/clientDisplay'
import LiveLogPanel from '@/components/ui/LiveLogPanel.vue'
import LiveStreamPanel from '@/components/ui/LiveStreamPanel.vue'
import {
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  ChevronUp,
  Eraser,
  ListChecks,
  Loader2,
  ScanText,
  X,
  XCircle,
} from 'lucide-vue-next'

import ParseChapterTable from '@/views/scriptparse/ParseChapterTable.vue'
import ParseDetailPanel from '@/views/scriptparse/ParseDetailPanel.vue'
import ParseChecksDialog from '@/views/scriptparse/ParseChecksDialog.vue'
import Pager from '@/views/textformat/Pager.vue'

const router = useRouter()
const { projectSet } = useProjectGate()
const submissionProject = useProjectStore()

const {
  state, stateError, summaryError, controlsReady, loading, submitting,
  rows, filteredRows, filteredTotal, pagedRows, pageCount, page, pageSize,
  filter, query, chapterNumPad,
  total, doneCount, pendingCount, busy,
  selected, selectedCount, selectedDoneCount, selectedStaleCount,
  selectScope, selectFiltered, clearSelection, toggleSelect,
  selectedName, selectChapter, currentRow, tab, setTab,
  resultPreview, sourcePreview,
  canSubmit, startParse, cancelAll, retryRow, cancelRow,
  checks, refreshState,
} = useScriptParseWorkbench()

// 管理员统一控制所有客户端的详细输出。
const clientDisplay = useClientDisplayStore()
const showParseLogs = computed(() => clientDisplay.logsEnabled)
const checksOpen = ref(false)
const logsExpanded = ref(false)

// --- source / stats display ----------------------------------------------------
const sourceMode = computed(() => state.value?.source.mode ?? 'empty')
const staleVersion = computed(
  () => sourceMode.value === 'version' && state.value!.source.version?.version_status === 'stale',
)

const activeRows = computed(() => rows.value.filter((r) => r.status === 'active'))

const bottomHint = computed(() => {
  if (sourceMode.value === 'empty') return '尚未生成分册文本，请先到「排版与分册」生成章节'
  if (staleVersion.value) return '该版本分册文本已被覆盖，请先到「排版与分册」刷新后重新解析'
  if (state.value?.text_format_busy) return '排版与分册任务进行中，暂不能提交解析'
  if (stateError.value) return '状态待确认，当前显示的是上次已知状态——可尝试刷新'
  const parts: string[] = []
  if (selectedDoneCount.value) parts.push(`${selectedDoneCount.value} 章已完成将重新解析`)
  if (selectedStaleCount.value) parts.push(`${selectedStaleCount.value} 章输入已变更将按最新内容解析`)
  if (busy.value) parts.push(`${activeRows.value.length} 章解析进行中`)
  return parts.join(' · ') || '按所选范围提交解析任务'
})

const startDisabled = computed(
  () => !projectSet.value || !state.value || busy.value || submitting.value || !selectedCount.value || !canSubmit.value,
)
const filteredEligibleCount = computed(() => filteredRows.value.filter(row => row.status !== 'active').length)
const canSelectScope = computed(() => projectSet.value && !busy.value && !loading.value && !stateError.value && total.value > 0)

// 下拉里的其余范围：选完即回空位（原生 select 同值不触发 change）。
function onScopeChange(e: Event) {
  const el = e.target as HTMLSelectElement
  const v = el.value
  el.value = ''
  if (v === 'all' || v === 'done' || v === 'failed') selectScope(v)
}

// --- narrow layout (drawer) ----------------------------------------------------
const isNarrow = ref(false)
const drawerOpen = ref(false)
const drawerPanel = ref<HTMLElement | null>(null)
let drawerReturnFocus: HTMLElement | null = null
let mediaMql: MediaQueryList | null = null
const mediaHandler = (e: MediaQueryListEvent) => {
  isNarrow.value = e.matches
  if (!e.matches) drawerOpen.value = false
}

function onChapterSelect(name: string) {
  selectChapter(name)
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

function onDrawerKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    drawerOpen.value = false
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
      <p class="eyebrow">Pipeline · LLM</p>
      <h1 class="page-title">文本解析</h1>
      <p class="page-description">按章节解析出角色与台词数据；可离开页面，稍后回来自动继续。</p>
    </header>

    <div class="workbench-controls" tabindex="0" role="region" aria-label="制作条件与流程">
      <ProjectGateAlert />
      <BatchSubmissionRecovery :routes="[`/api/v1/projects/${submissionProject.activeProjectId}/script-parse/run`]" @committed="refreshState()" />

      <!-- 状态警示 -->
      <Alert v-if="stateError" variant="destructive">
        <AlertTriangle class="h-4 w-4 shrink-0" />
        <p>状态待确认：{{ stateError }}（当前显示的是上次已知状态，勾选/提交前建议先刷新）</p>
      </Alert>
      <Alert v-if="state && state.text_format_busy" variant="warning">
        <AlertTriangle class="h-4 w-4 shrink-0" />
        <p>排版与分册任务正在进行，完成后即可提交解析（提交被临时禁用）。</p>
      </Alert>
      <Alert v-if="staleVersion" variant="destructive">
        <AlertTriangle class="h-4 w-4 shrink-0" />
        <p>该版本分册文本已被后续处理覆盖，请先到「排版与分册」重新处理后再解析。</p>
      </Alert>

      <Alert v-if="summaryError" variant="destructive">{{ summaryError }} <Button variant="outline" size="sm" @click="refreshState">重试全书状态</Button></Alert>
      <!-- 统计栏 -->
      <WorkbenchContextBar v-if="state">
        <template #icon><Loader2 v-if="busy" class="animate-spin" /><ScanText v-else /></template>
        <template #title>{{ !controlsReady ? '正在读取全书状态' : busy ? '解析进行中' : total === 0 ? '尚无可解析章节' : (pendingCount ?? 0) > 0 ? '部分章节待解析' : '全部章节已解析' }}</template>
        <template #description>{{ busy ? '可离开页面，回来自动继续' : total === 0 ? '请先在「排版与分册」生成章节' : '勾选章节后点击底部「开始解析」' }}</template>
        <template #metrics>
          <div class="workbench-context-metric"><strong>{{ total }}</strong>全部章节</div>
          <div class="workbench-context-metric"><strong :class="{ '!text-amber-600 dark:!text-amber-400': (pendingCount ?? 0) > 0 }">{{ pendingCount ?? '…' }}</strong>待解析</div>
          <div class="workbench-context-metric"><strong>{{ doneCount ?? '…' }}</strong>已完成</div>
        </template>
        <template #actions>
          <WorkbenchStatus v-if="state.text_format_busy" variant="warning">排版与分册进行中</WorkbenchStatus>
          <WorkbenchStatus v-if="staleVersion" variant="destructive">版本已被覆盖</WorkbenchStatus>
          <Button variant="outline" size="sm" @click="checksOpen = true"><ListChecks class="h-4 w-4" />解析检查项</Button>
        </template>
      </WorkbenchContextBar>


    </div>

    <!-- 工作区 -->
    <div v-if="state" class="wb-workspace glass-panel flex min-h-0 flex-1 flex-col overflow-hidden">
      <div class="min-h-0 flex-1" :inert="drawerOpen || undefined">
        <div class="wb-grid h-full">
          <div class="wb-main flex min-h-0 flex-col">
            <WorkbenchToolbar
              v-model:query="query"
              v-model:filter="filter"
              placeholder="搜索章节号或标题"
              :filters="[{ key: 'all', label: '全部', count: total }, { key: 'pending', label: '待解析', count: pendingCount ?? undefined }, { key: 'done', label: '已完成', count: doneCount ?? undefined }]"
              :loading="loading"
              :refresh-disabled="!projectSet"
              @refresh="refreshState()"
            >
              <template #selection>
                <Button variant="ghost" size="sm" :disabled="!canSelectScope || loading || !!stateError || !filteredEligibleCount" @click="selectFiltered">
                  选择筛选结果（{{ filteredEligibleCount }}）
                </Button>
                <Button variant="ghost" size="sm" :disabled="!canSelectScope" @click="selectScope('pending')">
                  <ListChecks class="h-3.5 w-3.5" />选择待解析
                </Button>
                <select
                  class="h-8 cursor-pointer rounded-md border bg-background px-1.5 text-xs"
                  aria-label="其他选择范围"
                  :disabled="!canSelectScope"
                  @change="onScopeChange($event)"
                >
                  <option value="" disabled selected>其他范围…</option>
                  <option value="all">全部章节（含已完成）</option>
                  <option value="done">仅已完成</option>
                  <option value="failed">仅失败</option>
                </select>
                <Button variant="ghost" size="sm" :disabled="!selectedCount" @click="clearSelection">
                  <Eraser class="h-3.5 w-3.5" />清空选择
                </Button>
              </template>
            </WorkbenchToolbar>
            <div class="min-h-0 flex-1 overflow-y-auto">
              <ParseChapterTable
                :rows="pagedRows"
                :selected="selected"
                :selected-name="selectedName"
                :num-pad="chapterNumPad"
                :page-size="pageSize"
                :empty-message="total === 0 ? '尚未生成分册文本，请先在「排版与分册」生成章节。' : filter === 'pending' ? '当前没有待解析章节' : '没有符合条件的章节'"
                :select-disabled="busy"
                @select="onChapterSelect"
                @toggle="toggleSelect"
                @retry="retryRow"
                @cancel="cancelRow"
              />
            </div>
            <Pager
              class="border-t px-4 py-2"
              :page="page"
              :page-count="pageCount"
              :total="filteredTotal"
              :page-size="pageSize"
              :page-size-options="[10, 20, 50]"
              @update:page="(p: number) => (page = p)"
              @update:page-size="(s: number) => { pageSize = s; page = 1 }"
            />
          </div>
          <aside v-if="!isNarrow" class="wb-detail min-h-0 border-l bg-muted/15" aria-label="章节预览">
            <ParseDetailPanel
              :row="currentRow"
              :tab="tab"
              :result-preview="resultPreview"
              :source-preview="sourcePreview"
              :num-pad="chapterNumPad"
              @update:tab="setTab($event)"
            />
          </aside>
        </div>
      </div>
    </div>

    <!-- 管理员开启客户端日志且有在途任务时显示 -->
    <div v-if="showParseLogs && activeRows.length" class="glass-panel shrink-0 overflow-hidden">
      <button
        type="button"
        class="flex w-full items-center gap-2 px-4 py-2.5 text-left text-sm font-medium hover:bg-accent/50"
        :aria-expanded="logsExpanded"
        @click="logsExpanded = !logsExpanded"
      >
        <ChevronUp v-if="logsExpanded" class="h-4 w-4 shrink-0" />
        <ChevronDown v-else class="h-4 w-4 shrink-0" />
        实时日志（{{ activeRows.length }} 章解析中）
      </button>
      <div v-if="logsExpanded" class="max-h-[36vh] divide-y overflow-y-auto">
        <div v-for="row in activeRows" :key="row.taskId ?? row.chapter.name" class="flex flex-wrap items-stretch gap-3 p-3">
          <div class="min-w-0 shrink-0 basis-48">
            <p class="truncate text-sm font-medium" :title="row.chapter.name">第{{ row.chapter.numStr }}章</p>
            <p class="text-xs text-muted-foreground">{{ row.label }} · {{ Math.round(row.progress * 100) }}%</p>
          </div>
          <div class="min-w-0 flex-1">
            <LiveLogPanel :task="row.task ?? null" :show-progress="false" :max-height-class="'h-28'" />
          </div>
          <div class="min-w-0 flex-1">
            <LiveStreamPanel :task="row.task ?? null" :max-height-class="'h-28'" />
          </div>
        </div>
      </div>
    </div>

    <!-- 底栏：选择摘要与执行操作 -->
    <WorkbenchActionBar>
      <template #summary>
        <strong>已选 {{ selectedCount }} 章</strong>
        <p class="mt-1 text-muted-foreground">{{ bottomHint }}</p>
      </template>
      <Button v-if="busy" variant="outline" size="sm" class="text-red-600 hover:border-red-500/40 hover:text-red-600 dark:text-red-400 dark:hover:text-red-400" @click="cancelAll">
        <XCircle class="h-4 w-4" />取消全部
      </Button>
      <Button size="sm" :disabled="startDisabled" @click="startParse()">
        <Loader2 v-if="submitting" class="h-4 w-4 animate-spin" />
        <ScanText v-else class="h-4 w-4" />
        {{ submitting ? '提交中…' : selectedCount > 0 ? `开始解析（${selectedCount} 章）` : '开始解析' }}
      </Button>
      <Button
        variant="outline"
        size="sm"
        :disabled="!controlsReady || !doneCount"
        :title="!doneCount ? '还没有可配音的解析结果' : '前往角色配音'"
        @click="router.push('/voices')"
      >
        前往角色配音
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
          aria-label="章节预览"
          class="absolute right-0 top-0 flex h-full w-[85vw] max-w-[380px] flex-col border-l bg-background shadow-xl"
          @keydown="onDrawerKeydown"
        >
          <header class="flex items-center justify-between border-b px-4 py-3">
            <span class="text-sm font-semibold">章节预览</span>
            <Button variant="ghost" size="icon" class="h-7 w-7" aria-label="关闭详情" @click="drawerOpen = false">
              <X class="h-4 w-4" />
            </Button>
          </header>
          <div class="min-h-0 flex-1">
            <ParseDetailPanel
              :row="currentRow"
              :tab="tab"
              :result-preview="resultPreview"
              :source-preview="sourcePreview"
              :num-pad="chapterNumPad"
              @update:tab="setTab($event)"
            />
          </div>
        </section>
      </div>
    </Teleport>

    <ParseChecksDialog :open="checksOpen" :checks="checks" :busy="busy" @close="checksOpen = false" />
  </div>
</template>

<style scoped>
/* 定高撑满视口（同排版与分册工作台）：工作区 flex-1 吃掉空隙，底栏留 8px 不贴死。 */
.wb-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.wb-page .page-header {
  margin-bottom: 8px;
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
