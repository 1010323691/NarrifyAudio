<script setup lang="ts">
import { vFitRows } from '@/directives/fitRows'
import Pager from '@/views/textformat/Pager.vue'
import { computed, nextTick, onBeforeUnmount, onDeactivated, ref, watch } from 'vue'
import {
  Activity, AudioLines, Check, ChevronRight, CircleAlert, Clock3, Combine,
  FileText, Folder, LoaderCircle, ListTodo, Music4, Pause, Play, RefreshCw,
  ScanText, Users, X,
} from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import Progress from '@/components/ui/Progress.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useTaskCenter } from '@/composables/useTaskCenter'
import type { TaskCenterGroup } from '@/api/tasks'
import type { TaskCenterItem, TaskStatus } from '@/types'
import { TASK_CENTER_CATEGORIES } from '@/utils/taskCenter'

const center = useTaskCenter()
const {
  sections, browsingSection, groupPage, groupPageSize, selectedGroup, taskFilter, taskPage, taskPageSize, selectedCounts,
  summary, summaryLoading, summaryError, groups, groupsLoading, groupsError,
  items, itemsLoading, itemsError, controllingCategory, controlError,
} = center
const groupPageCount = computed(() => Math.max(1, Math.ceil((groups.value?.total ?? 0) / groupPageSize.value)))
const taskPageCount = computed(() => Math.max(1, Math.ceil((items.value?.total ?? selectedCounts.value?.task_count ?? 0) / taskPageSize.value)))
const selectedCategory = computed(() => TASK_CENTER_CATEGORIES.find(value => value.id === selectedGroup.value?.categoryId))
const selectedProjectName = computed(() => selectedGroup.value?.initial.project_name || '未知书籍')
const taskFilters = [
  { id: 'all', label: '全部' },
  { id: 'active', label: '进行中' },
  { id: 'completed', label: '已完成' },
] as const
const dialogPanel = ref<HTMLElement | null>(null)
const dialogCloseButton = ref<HTMLButtonElement | null>(null)
let restoreFocus: HTMLElement | null = null

function statusInfo(status: TaskStatus): { label: string; tone: 'positive' | 'warning' | 'negative' | 'neutral' } {
  if (status === 'succeeded') return { label: '已完成', tone: 'positive' }
  if (status === 'failed') return { label: '失败', tone: 'negative' }
  if (status === 'timeout') return { label: '超时', tone: 'negative' }
  if (status === 'cancelled') return { label: '已取消', tone: 'neutral' }
  if (status === 'running' || status === 'cancelling') return { label: '进行中', tone: 'warning' }
  if (status === 'paused') return { label: '已暂停', tone: 'neutral' }
  return { label: '排队中', tone: 'neutral' }
}

function taskIcon(taskType: string) {
  if (taskType === 'script.parse') return ScanText
  if (taskType.startsWith('voices.')) return Users
  if (taskType === 'tts.batch') return AudioLines
  if (taskType === 'tts.merge') return Combine
  if (taskType.startsWith('bgm.')) return Music4
  return FileText
}

function taskDetail(task: TaskCenterItem) {
  if (task.status === 'paused') {
    if (task.error_code === 'manual_pause' || task.error === '用户已暂停任务') return task.current || '已暂停，可在当前任务列表中点击启动全部继续'
    return task.current || task.error || '等待 LLM 服务恢复，系统会每分钟检查并自动重试'
  }
  if (task.current) return task.current
  if (task.error) return task.error
  if (task.status === 'succeeded') return '任务已完成'
  if (task.status === 'failed') return '任务执行失败'
  if (task.status === 'timeout') return '任务超时'
  if (task.status === 'cancelled') return '任务已取消'
  return '等待状态更新'
}

function formatTime(value: number) {
  if (!value) return '时间未知'
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
    .format(new Date(value * 1000))
}

function openGroup(group: TaskCenterGroup, event: MouseEvent) {
  restoreFocus = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  center.openGroup(group)
  void nextTick(() => dialogCloseButton.value?.focus())
}

function closeDialog(returnFocus = true) {
  center.closeGroup()
  if (returnFocus) void nextTick(() => restoreFocus?.focus())
}

function handleDialogKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeDialog()
    return
  }
  if (event.key !== 'Tab' || !dialogPanel.value) return
  const focusable = [...dialogPanel.value.querySelectorAll<HTMLElement>(
    'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
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

// Pagination can remove the focused row/button while a request is pending.
// Escape must still close the active dialog when focus has fallen to the body.
function handleEscape(event: KeyboardEvent) {
  if (selectedGroup.value && event.key === 'Escape' && !event.defaultPrevented) {
    event.preventDefault()
    closeDialog()
  }
}
watch(() => !!selectedGroup.value, (open) => {
  if (open) document.addEventListener('keydown', handleEscape)
  else document.removeEventListener('keydown', handleEscape)
}, { flush: 'sync' })
onBeforeUnmount(() => document.removeEventListener('keydown', handleEscape))
onDeactivated(() => closeDialog(false))
</script>

<template>
  <div class="task-center viewport-page">
    <header class="page-header">
      <div class="task-center__header">
        <div>
          <p class="eyebrow">CROSS-PROJECT OVERVIEW</p>
          <h1 class="page-title">任务中心</h1>
          <p class="page-description">集中查看各本书的制作任务和实时进度。</p>
        </div>
      </div>
    </header>

    <div class="page-region" role="region" aria-label="页面工作区" tabindex="0">
      <div v-if="summaryError" class="task-center__error" role="alert">
        <CircleAlert class="h-4 w-4 shrink-0" /><span>摘要暂未更新：{{ summaryError }}</span>
        <Button variant="outline" size="sm" @click="center.refreshSummary()">重试</Button>
      </div>

      <div class="task-center__workspace">
        <nav class="task-center__stages" aria-label="制作阶段">
          <div class="task-center__nav-head">
            <p class="task-center__nav-label">制作阶段</p>
            <Button variant="ghost" size="sm" :disabled="summaryLoading" @click="center.refreshAll()">
              <RefreshCw class="h-4 w-4" :class="summaryLoading ? 'animate-spin' : ''" />刷新
            </Button>
          </div>
          <button v-for="(section, index) in sections" :key="section.id" type="button"
            class="task-center__stage" :class="{ 'is-selected': browsingSection.id === section.id }"
            :aria-pressed="browsingSection.id === section.id" @click="center.selectCategory(section.id)">
            <span class="task-center__stage-number">{{ String(index + 1).padStart(2, '0') }}</span>
            <span class="task-center__stage-copy"><strong>{{ section.label }}</strong><small>{{ summary ? section.project_count : '…' }} 个项目<span v-if="section.active_count > 0"> · 进行中</span></small></span>
            <span class="task-center__stage-count">{{ summary ? section.task_count : '…' }}</span>
          </button>
        </nav>
        <Card class="task-center__section task-center__stage-detail">
          <div class="task-center__section-head">
            <div class="task-center__section-icon" aria-hidden="true">
              <ListTodo class="h-4 w-4" />
            </div>
            <div class="task-center__section-copy">
              <h2>{{ browsingSection.label }}</h2>
              <p>{{ browsingSection.description }} · 点击项目查看子任务与批量控制</p>
            </div>
            <span class="task-center__section-count">{{ summary ? browsingSection.task_count : '…' }} 项任务</span>
          </div>

          <div v-if="groupsError" class="task-center__error" role="alert">
            项目列表暂未更新：{{ groupsError }}
            <Button variant="ghost" size="sm" @click="center.refreshGroups()">重试</Button>
          </div>
          <div v-if="!groups" class="task-center__loading" role="status">
            <LoaderCircle v-if="groupsLoading || summaryLoading" class="h-5 w-5 animate-spin" />{{ groupsLoading || summaryLoading ? '正在读取项目摘要…' : '项目摘要尚未加载' }}
          </div>
          <div v-else-if="groups.items.length" class="task-center__folder-list" :class="{ 'is-scroll': groupPageSize > 10 }" :aria-busy="groupsLoading" v-fit-rows="{ prop: '--tc-row', max: 90, min: 62 }">
            <button
              v-for="group in groups.items"
              :key="group.project_id"
              type="button"
              class="task-center__folder"
              :aria-label="`${group.project_name}，${group.task_count} 项任务，打开子任务`"
              @click="openGroup(group, $event)"
            >
              <span class="task-center__folder-icon" aria-hidden="true"><Folder class="h-5 w-5" /></span>
              <span class="task-center__folder-copy">
                <strong>{{ group.project_name }}</strong>
                <small>{{ group.task_count }} 项任务 · 最近提交 {{ formatTime(group.latest) }}</small>
              </span>
              <span class="task-center__group-progress"><strong>{{ group.succeeded_count }} / {{ group.task_count }}</strong><small>已完成</small></span>
              <StatusPill v-if="group.active_count" :label="`${group.active_count} 项进行中`" tone="warning" />
              <StatusPill v-else :label="statusInfo(group.latest_status).label" :tone="statusInfo(group.latest_status).tone" />
              <ChevronRight class="task-center__folder-arrow h-4 w-4" aria-hidden="true" />
            </button>
          </div>
          <div v-else class="task-center__empty-section">
            <ListTodo class="h-7 w-7" aria-hidden="true" />
            <strong>此阶段还没有任务</strong>
            <span>在项目中提交制作任务后，可在这里跟踪进度。</span>
          </div>
          <Pager class="shrink-0 border-t px-3 py-2" :page="groupPage" :page-count="groupPageCount" :total="groups?.total ?? 0" :page-size="groupPageSize" unit="个项目" @update:page="groupPage = $event" @update:page-size="(s: number) => { groupPageSize = s; groupPage = 1 }" />
        </Card>
      </div>
    </div>

    <div v-if="selectedGroup && selectedCategory" class="task-center__overlay" @click.self="closeDialog()">
      <section
        ref="dialogPanel"
        class="task-center__dialog"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="'task-center-dialog-title'"
        @keydown="handleDialogKeydown"
      >
        <header class="task-center__dialog-head">
          <div>
            <p class="eyebrow">{{ selectedCategory.label }}</p>
            <h2 id="task-center-dialog-title">{{ selectedProjectName }}</h2>
            <p>{{ selectedCounts?.task_count ?? 0 }} 项子任务</p>
          </div>
          <div class="task-center__dialog-actions">
            <button ref="dialogCloseButton" type="button" class="task-center__close" aria-label="关闭子任务窗口" @click="closeDialog()">
              <X class="h-5 w-5" aria-hidden="true" />
            </button>
            <div class="task-center__batch-actions" role="group" :aria-label="`${selectedCategory.label}批量控制`">
              <Button
                variant="outline"
                size="sm"
                class="task-center__batch-control"
                :disabled="!selectedCounts?.pausable_count || !!controllingCategory"
                :aria-busy="controllingCategory === selectedGroup.categoryId"
                :aria-label="`${selectedProjectName}的${selectedCategory.label}：暂停全部任务`"
                @click="center.control('pause')"
              >
                <LoaderCircle v-if="controllingCategory === selectedGroup.categoryId" class="h-4 w-4 animate-spin" />
                <Pause v-else class="h-4 w-4" />
                暂停全部
              </Button>
              <Button
                variant="outline"
                size="sm"
                class="task-center__batch-control"
                :disabled="!selectedCounts?.resumable_count || !!controllingCategory"
                :aria-busy="controllingCategory === selectedGroup.categoryId"
                :aria-label="`${selectedProjectName}的${selectedCategory.label}：启动全部任务`"
                @click="center.control('resume')"
              >
                <LoaderCircle v-if="controllingCategory === selectedGroup.categoryId" class="h-4 w-4 animate-spin" />
                <Play v-else class="h-4 w-4" />
                启动全部
              </Button>
              <Button
                variant="outline"
                size="sm"
                class="task-center__batch-control text-destructive"
                :disabled="!selectedCounts?.active_count || !!controllingCategory"
                :aria-label="`${selectedProjectName}的${selectedCategory.label}：取消此批全部任务`"
                @click="center.control('cancel')"
              >
                <LoaderCircle v-if="controllingCategory === selectedGroup.categoryId" class="h-4 w-4 animate-spin" />
                <X v-else class="h-4 w-4" />
                取消全部
              </Button>
            </div>
            <div class="task-center__filter" role="group" aria-label="按任务状态筛选">
              <span class="task-center__filter-thumb" :class="`is-${taskFilter}`" aria-hidden="true" />
              <button
                v-for="filter in taskFilters"
                :key="filter.id"
                type="button"
                :aria-pressed="taskFilter === filter.id"
                @click="center.selectFilter(filter.id)"
              >{{ filter.label }}</button>
            </div>
          </div>
        </header>

      <div v-if="controlError" class="task-center__error" role="alert">
        <CircleAlert class="h-4 w-4 shrink-0" /><span>{{ controlError }}</span>
        <Button variant="ghost" size="sm" aria-label="关闭批量控制错误提示" @click="controlError = ''"><X class="h-4 w-4" /></Button>
      </div>

        <div v-if="itemsError" class="task-center__error" role="alert">
          任务列表暂未更新：{{ itemsError }}
          <Button variant="ghost" size="sm" @click="center.refreshItems()">重试</Button>
        </div>
        <div v-if="!items" class="task-center__loading" role="status">
          <LoaderCircle v-if="itemsLoading" class="h-5 w-5 animate-spin" />{{ itemsLoading ? '正在读取当前页任务…' : '任务列表尚未加载' }}
        </div>
        <div v-else-if="items.items.length" class="task-center__task-list" :class="{ 'is-scroll': taskPageSize > 10 }" :aria-busy="itemsLoading" v-fit-rows="{ prop: '--tk-row', max: 52, min: 44 }">
          <article v-for="task in items.items" :key="task.id" class="task-center__task-row">
            <div class="task-center__task-icon" aria-hidden="true"><component :is="taskIcon(task.task_type)" class="h-4 w-4" /></div>
            <strong class="task-center__task-title" :title="task.label">{{ task.label }}</strong>
            <time class="task-center__task-time">{{ formatTime(task.created) }}</time>
            <div class="task-center__task-progress">
              <Progress :value="task.progress" :aria-label="`${task.label}进度`" class="h-2" />
              <span>{{ Math.round(task.progress * 100) }}%</span>
            </div>
            <p class="task-center__task-detail" :class="{ 'is-error': !task.current && !!task.error }" :title="taskDetail(task)">
              <Activity v-if="task.current" class="h-3.5 w-3.5" />
              <CircleAlert v-else-if="task.error" class="h-3.5 w-3.5" />
              <Check v-else-if="task.status === 'succeeded'" class="h-3.5 w-3.5" />
              <Clock3 v-else class="h-3.5 w-3.5" />
              <span>{{ taskDetail(task) }}</span>
            </p>
            <StatusPill :label="statusInfo(task.status).label" :tone="statusInfo(task.status).tone" />
          </article>
        </div>
        <div v-else class="task-center__dialog-empty">
          <ListTodo class="h-8 w-8" aria-hidden="true" />
          <p>{{ selectedCounts?.task_count ? '当前筛选下没有子任务。' : '此书籍在该分区还没有子任务。' }}</p>
        </div>
        <Pager class="shrink-0 border-t px-3 py-2" :page="taskPage" :page-count="taskPageCount" :total="items?.total ?? selectedCounts?.task_count ?? 0" :page-size="taskPageSize" unit="项任务" @update:page="taskPage = $event" @update:page-size="(s: number) => { taskPageSize = s; taskPage = 1 }" />
      </section>
    </div>
  </div>
</template>

<style scoped>
.task-center{display:grid;gap:18px;max-width:1320px;margin:0 auto;padding-bottom:30px}.task-center__header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}.task-center__sections{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.task-center__section{min-width:0;padding:15px}.task-center__section-head{display:flex;align-items:center;gap:10px}.task-center__section-icon{display:grid;place-items:center;width:34px;height:34px;flex:none;border:1px solid hsl(var(--primary)/.12);border-radius:10px;background:hsl(var(--primary)/.08);color:hsl(var(--primary))}.task-center__section-copy{min-width:0;flex:1}.task-center__section-copy h2{font-size:13px;font-weight:750;line-height:1.4}.task-center__section-copy p{margin-top:2px;color:hsl(var(--muted-foreground));font-size:10px}.task-center__section-count{flex:none;color:hsl(var(--muted-foreground));font-size:10px;font-variant-numeric:tabular-nums}.task-center__folder-list{display:grid;gap:6px;margin-top:13px}.task-center__folder{display:flex;align-items:center;gap:10px;width:100%;height:60px;overflow:hidden;border:1px solid hsl(var(--border)/.75);border-radius:11px;background:var(--glass-tint);padding:9px 10px;text-align:left;transition:border-color .15s,background .15s,transform .15s}.task-center__folder:hover{transform:translateY(-1px);border-color:hsl(var(--primary)/.35);background:hsl(var(--primary)/.04)}.task-center__folder:focus-visible,.task-center__close:focus-visible{outline:2px solid hsl(var(--ring));outline-offset:2px}.task-center__folder-icon{display:grid;place-items:center;width:34px;height:34px;flex:none;border-radius:9px;background:hsl(var(--primary)/.08);color:hsl(var(--primary))}.task-center__folder-copy{display:grid;gap:2px;min-width:0;flex:1}.task-center__folder-copy strong{overflow:hidden;font-size:12px;text-overflow:ellipsis;white-space:nowrap}.task-center__folder-copy small{color:hsl(var(--muted-foreground));font-size:10px}.task-center__folder-arrow{flex:none;color:hsl(var(--muted-foreground))}.task-center__empty-section{display:grid;place-items:center;min-height:74px;margin-top:11px;border:1px dashed hsl(var(--border));border-radius:10px;color:hsl(var(--muted-foreground));font-size:11px}.task-center__loading{display:flex;align-items:center;justify-content:center;gap:9px;min-height:220px;color:hsl(var(--muted-foreground));font-size:13px}.task-center__error{display:flex;align-items:center;gap:9px;border:1px solid hsl(var(--destructive)/.2);border-radius:11px;background:hsl(var(--destructive)/.05);padding:10px 12px;color:hsl(var(--destructive));font-size:12px}.task-center__error span{flex:1}.task-center__more{display:flex;justify-content:center}.task-center__overlay{position:fixed;inset:0;z-index:80;display:grid;place-items:center;overflow:auto;background:rgba(18,20,37,.48);padding:22px}.task-center__dialog{display:flex;flex-direction:column;height:min(82vh,820px);overflow:hidden;border:1px solid var(--glass-border);border-radius:18px;background:hsl(var(--card)/.95);box-shadow:var(--glass-highlight),var(--glass-shadow-strong);backdrop-filter:none}.task-center__dialog-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;border-bottom:1px solid hsl(var(--border)/.75);padding:20px 22px}.task-center__dialog-head h2{margin-top:3px;font-size:20px;font-weight:760;overflow-wrap:anywhere}.task-center__dialog-head p:last-child{margin-top:3px;color:hsl(var(--muted-foreground));font-size:11px}.task-center__close{display:grid;place-items:center;width:40px;height:40px;flex:none;border:1px solid hsl(var(--border));border-radius:10px;background:hsl(var(--background)/.65);color:hsl(var(--muted-foreground));transition:color .14s,border-color .14s}.task-center__close:hover{border-color:hsl(var(--primary)/.35);color:hsl(var(--foreground))}.task-center__task-list{flex:none;box-sizing:border-box;height:calc(10 * var(--tk-row,52px));display:grid;align-content:start;grid-auto-rows:var(--tk-row,52px);gap:0;min-height:0;overflow-y:hidden}.task-center__task-list.is-scroll{overflow-y:auto}.task-center__task-row{border-bottom:1px solid hsl(var(--border)/.72)}.task-center__task-row:last-child{border-bottom:0}.task-center__task-icon{display:grid;place-items:center;flex:none;border-radius:9px;background:hsl(var(--primary)/.08);color:hsl(var(--primary))}.task-center__task-progress{display:flex;align-items:center}.task-center__task-progress>div{flex:1}.task-center__task-progress>span{color:hsl(var(--muted-foreground));font-size:10px;text-align:right;font-variant-numeric:tabular-nums}.task-center__dialog-empty{display:grid;place-items:center;align-content:center;gap:8px;flex:1;min-height:0;color:hsl(var(--muted-foreground));font-size:12px}.task-center__dialog-empty svg{color:hsl(var(--primary))}@supports not (backdrop-filter:blur(1px)){.task-center__dialog{background:hsl(var(--background))}}@media(max-width:900px){.task-center__sections{grid-template-columns:1fr}}@media(max-width:520px){.task-center__header{align-items:flex-start}.task-center__header>button{width:100%}.task-center__section{padding:12px}.task-center__section-head{gap:8px}.task-center__section-copy h2{font-size:12px}.task-center__section-count{font-size:9px}.task-center__overlay{padding:10px}.task-center__dialog{height:90vh;border-radius:14px}.task-center__dialog-head{padding:16px}}
/* Keep each task readable as a single horizontal entry. Narrow screens can scroll the row. */
.task-center__dialog{width:min(1120px,100%)}
.task-center__task-list{overflow-x:auto;padding:0 18px}
/* 标题/时间/详情/状态列全部固定宽度（内容过长省略号截断，hover 可见全文），
 * 保证各行时间、进度条、详情、状态列纵向对齐，且行宽不足压缩时不因行内容
 * 宽度不同产生错位；进度条列 1fr 吃满剩余空间。
 * 时间列 64px（实测 MM/DD HH:mm 约 52px）；状态列 74px（最宽 3 汉字胶囊
 * "已完成" 实测 68px，留 6px 余量）。标题 display:block 是必需的：
 * 省略号只对块级容器渲染。 */
.task-center__task-row{display:grid;grid-template-columns:32px minmax(0,380px) 64px minmax(180px,1fr) minmax(0,150px) 74px;align-items:center;gap:12px;width:100%;min-width:900px;height:var(--tk-row,52px);padding:0;overflow:hidden;box-sizing:border-box}
.task-center__task-icon{width:30px;height:30px}
.task-center__task-title{display:block;min-width:0;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-center__task-time{color:hsl(var(--muted-foreground));font-size:10px;white-space:nowrap;font-variant-numeric:tabular-nums}
.task-center__task-progress{width:auto;min-width:180px;gap:8px;margin:0}
.task-center__task-progress>div{min-width:80px}
.task-center__task-progress>span{flex:none;width:34px;white-space:nowrap}
.task-center__task-detail{display:flex;align-items:center;gap:5px;min-width:0;margin:0;color:hsl(var(--muted-foreground));font-size:10px;white-space:nowrap}
.task-center__task-detail svg{flex:none;color:hsl(var(--primary))}
.task-center__task-detail span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-center__task-detail.is-error,.task-center__task-detail.is-error svg{color:hsl(var(--destructive))}
.task-center__task-row>:last-child{justify-self:end;white-space:nowrap}
@media(max-width:520px){.task-center__task-list{padding:0 12px}.task-center__task-row{gap:9px}}
.task-center__dialog-actions{display:flex;flex-direction:column;align-items:flex-end;gap:12px;flex:none}
.task-center__filter{position:relative;display:grid;grid-template-columns:repeat(3,minmax(72px,1fr));align-items:center;gap:2px;border:1px solid hsl(var(--border)/.85);border-radius:10px;background:hsl(var(--background)/.58);padding:3px;isolation:isolate}
.task-center__filter-thumb{position:absolute;z-index:-1;top:3px;bottom:3px;left:3px;width:calc((100% - 10px)/3);border:1px solid hsl(var(--primary)/.16);border-radius:7px;background:hsl(var(--primary)/.12);box-shadow:0 1px 2px hsl(var(--foreground)/.08);transition:transform .2s ease}
.task-center__filter-thumb.is-active{transform:translateX(calc(100% + 2px))}
.task-center__filter-thumb.is-completed{transform:translateX(calc(200% + 4px))}
.task-center__filter button{position:relative;z-index:1;min-height:29px;border-radius:7px;padding:0 10px;color:hsl(var(--muted-foreground));font-size:11px;white-space:nowrap;transition:color .16s}
.task-center__filter button[aria-pressed=true]{color:hsl(var(--foreground));font-weight:650}
.task-center__filter button:focus-visible{outline:2px solid hsl(var(--ring));outline-offset:1px}
@media(max-width:520px){.task-center__dialog-head{gap:10px}.task-center__dialog-actions{gap:8px}.task-center__filter{grid-template-columns:repeat(3,minmax(58px,1fr))}.task-center__filter button{padding:0 6px}}
.task-center__batch-actions{display:flex;align-items:center;gap:7px}
.task-center__batch-control{flex:none;min-height:36px;gap:7px}
@media(max-width:520px){.task-center__dialog-actions{flex-wrap:wrap;justify-content:flex-end}.task-center__batch-actions{flex-wrap:wrap;justify-content:flex-end}}
.task-center__workspace { display:grid; grid-template-columns:260px minmax(0,1fr); grid-template-rows:minmax(0,1fr); gap:16px; align-items:stretch; min-width:0; min-height:0; flex:1; }
.task-center { width:100%; }
.task-center__stages { padding:8px; border:1px solid hsl(var(--border)); border-radius:12px; background:hsl(var(--card)); }
.task-center__nav-head { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:0 0 6px 10px; }
.task-center__nav-label { font-size:11px; font-weight:600; color:hsl(var(--muted-foreground)); }
.task-center__stage { display:flex; align-items:center; gap:10px; width:100%; padding:13px 10px; border-radius:8px; text-align:left; color:hsl(var(--muted-foreground)); transition:background .15s; }
.task-center__stage:hover { background:hsl(var(--muted)/.6); }
.task-center__stage.is-selected { background:hsl(var(--primary)/.08); color:hsl(var(--primary)); }
.task-center__stage:focus-visible { outline:2px solid hsl(var(--ring)); outline-offset:2px; }
.task-center__stage-number { font-size:11px; font-variant-numeric:tabular-nums; opacity:.7; }
.task-center__stage-copy { min-width:0; flex:1; display:grid; gap:4px; }
.task-center__stage-copy strong { font-size:12px; font-weight:600; line-height:1.5; }
.task-center__stage-copy small { font-size:11px; color:hsl(var(--muted-foreground)); }
.task-center__stage-count { font-size:12px; font-variant-numeric:tabular-nums; }
.task-center__stage-detail { display:flex; flex-direction:column; min-width:0; min-height:0; padding:0; overflow:hidden; border-radius:12px; }
.task-center__stage-detail .task-center__section-head { flex-shrink:0; padding:20px; border-bottom:1px solid hsl(var(--border)); }
.task-center__stage-detail .task-center__section-copy h2 { font-size:15px; }
.task-center__stage-detail .task-center__section-copy p { margin-top:5px; font-size:12px; }
.task-center__stage-detail .task-center__folder-list { display:flex; flex-direction:column; flex:none; box-sizing:border-box; height:calc(10 * var(--tc-row,90px)); overflow-y:hidden; margin:0; gap:0; }
.task-center__stage-detail .task-center__folder-list.is-scroll { overflow-y:auto; }
.task-center__stage-detail .task-center__folder { flex:none; height:var(--tc-row,90px); border:0; border-bottom:1px solid hsl(var(--border)); border-radius:0; min-height:0; padding:18px 20px; background:transparent; }
.task-center__stage-detail .task-center__folder:last-child { border-bottom:0; }
.task-center__stage-detail .task-center__folder:hover { transform:none; background:hsl(var(--primary)/.035); }
.task-center__stage-detail .task-center__folder-copy strong { font-size:14px; font-weight:600; }
.task-center__stage-detail .task-center__folder-copy small { margin-top:5px; font-size:11px; }
.task-center__group-progress { display:grid; gap:4px; margin:0 20px; text-align:right; font-variant-numeric:tabular-nums; }
.task-center__group-progress strong { font-size:13px; font-weight:600; }
.task-center__group-progress small { font-size:11px; color:hsl(var(--muted-foreground)); }
.task-center__stage-detail .task-center__empty-section { flex:1; gap:10px; align-content:center; min-height:0; padding:24px; margin:0; border:0; }
.task-center__group-pages { display:flex; flex-shrink:0; align-items:center; justify-content:space-between; gap:8px; padding:8px 12px; border-top:1px solid hsl(var(--border)); color:hsl(var(--muted-foreground)); font-size:11px; }
.task-center__group-pages>div { display:flex; gap:4px; }
:global(.app-shell .app-content) .task-center > .page-region { display:flex; flex-direction:column; overflow:hidden; }
.task-center__empty-section strong { font-size:14px; font-weight:600; color:hsl(var(--foreground)); }
.task-center__empty-section svg { color:hsl(var(--muted-foreground)/.6); }
@media(max-width:1100px) {
  .task-center__workspace { grid-template-columns:220px minmax(0,1fr); gap:12px; }
  .task-center__group-progress { margin:0 6px; }
  .task-center__stage-detail .task-center__folder { padding:16px 12px; }
}
@media(max-width:700px) {
  .task-center__workspace { grid-template-columns:minmax(0,1fr); }
  .task-center__workspace { grid-template-rows:auto minmax(0,1fr); }
  .task-center__stages { display:flex; overflow:auto; gap:4px; }
  .task-center__stages { scrollbar-width:none; }
  .task-center__stages::-webkit-scrollbar { display:none; }
  .task-center__nav-head { flex-shrink:0; padding:0 8px; }
  .task-center__nav-label { display:none; }
  .task-center__stage { min-width:175px; width:auto; flex-shrink:0; }
  .task-center__group-progress { display:none; }
}
@media(prefers-reduced-motion:reduce) { .task-center__stage, .task-center__folder { transition:none; } }
</style>
