<script setup lang="ts">
import { computed, nextTick, onDeactivated, onActivated, ref } from 'vue'
import {
  Activity, AudioLines, Check, ChevronRight, CircleAlert, Clock3, Combine,
  FileText, Folder, LoaderCircle, ListTodo, Music4, Pause, Play, RefreshCw,
  ScanText, Users, X,
} from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import Progress from '@/components/ui/Progress.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { listTaskHistory } from '@/api/tasks'
import { useTaskStore } from '@/stores/task'
import type { TaskCenterItem, TaskStatus } from '@/types'
import { TASK_CENTER_CATEGORIES, taskCenterCategoryOf, type TaskCenterCategoryId } from '@/utils/taskCenter'

const taskStore = useTaskStore()
const historyTasks = ref<TaskCenterItem[]>([])
const cursor = ref<string | null>(null)
const loading = ref(false)
const loadingMore = ref(false)
const loaded = ref(false)
const error = ref('')
const controlError = ref('')
const controllingCategory = ref<TaskCenterCategoryId | null>(null)
const taskFilter = ref<'all' | 'active' | 'completed'>('all')
// A single bulk run can occupy the newest history pages (a 400-chapter
// re-parse spans 8+ pages of 50). Auto-load this many rows so recently
// completed entries stay in view instead of sitting behind manual
// "load earlier" clicks.
const AUTO_HISTORY_LIMIT = 500
const selectedGroup = ref<{ categoryId: TaskCenterCategoryId; projectId: string } | null>(null)
const dialogPanel = ref<HTMLElement | null>(null)
const dialogCloseButton = ref<HTMLButtonElement | null>(null)
let restoreFocus: HTMLElement | null = null

const allTasks = computed(() => {
  const byId = new Map<string, TaskCenterItem>()
  for (const task of historyTasks.value) byId.set(task.id, task)
  for (const task of taskStore.tasks) {
    if (task.project_name && task.task_type) byId.set(task.id, task)
  }
  return [...byId.values()]
    // 被新运行替换的终态行不再展示（服务端历史接口也不会再返回它们），
    // 历史页里已加载的旧条目同样要按此过滤，否则重跑后同名任务会成对出现。
    .filter((task) => task.status !== 'cancelled' && !taskStore.supersededIds.has(task.id))
    .sort((a, b) => b.created - a.created || b.id.localeCompare(a.id))
})

const sections = computed(() => TASK_CENTER_CATEGORIES.map((category) => {
  const projects = new Map<string, TaskCenterItem[]>()
  for (const task of allTasks.value) {
    if (taskCenterCategoryOf(task.task_type) !== category.id) continue
    const projectTasks = projects.get(task.project_id) ?? []
    projectTasks.push(task)
    projects.set(task.project_id, projectTasks)
  }
  const groups = [...projects.entries()].map(([projectId, tasks]) => ({
    projectId,
    projectName: tasks.find((task) => task.project_name)?.project_name || '未知书籍',
    tasks: tasks.sort((a, b) => b.created - a.created),
    latest: Math.max(...tasks.map((task) => task.created)),
    activeCount: tasks.filter((task) => isActive(task.status)).length,
  })).sort((a, b) => b.latest - a.latest || a.projectName.localeCompare(b.projectName))
  return {
    ...category,
    groups,
    taskCount: groups.reduce((sum, group) => sum + group.tasks.length, 0),
  }
}))

const selectedCategory = computed(() => selectedGroup.value
  ? TASK_CENTER_CATEGORIES.find((category) => category.id === selectedGroup.value?.categoryId) ?? null
  : null)
const selectedTasks = computed(() => {
  if (!selectedGroup.value) return []
  return allTasks.value
    .filter((task) => task.project_id === selectedGroup.value?.projectId
      && taskCenterCategoryOf(task.task_type) === selectedGroup.value?.categoryId)
    .sort((a, b) => a.created - b.created || a.id.localeCompare(b.id))
})
const filteredSelectedTasks = computed(() => selectedTasks.value.filter((task) => {
  if (taskFilter.value === 'active') return isActive(task.status)
  if (taskFilter.value === 'completed') return isCompleted(task.status)
  return true
}))
const selectedProjectName = computed(() => selectedTasks.value[0]?.project_name || '未知书籍')
const selectedActiveTasks = computed(() => selectedTasks.value.filter((task) =>
  ['pending', 'queued', 'retrying', 'running', 'paused'].includes(task.status),
))
const selectedPausableTasks = computed(() => selectedTasks.value.filter((task) =>
  ['pending', 'queued', 'retrying', 'running'].includes(task.status),
))
const selectedResumableTasks = computed(() => selectedTasks.value.filter((task) => task.status === 'paused'))

const taskFilters = [
  { id: 'all', label: '全部' },
  { id: 'active', label: '进行中' },
  { id: 'completed', label: '已完成' },
] as const

function isActive(status: TaskStatus) {
  return ['pending', 'queued', 'running', 'retrying', 'cancelling', 'paused'].includes(status)
}

function isCompleted(status: TaskStatus) {
  return ['succeeded', 'failed', 'timeout', 'cancelled'].includes(status)
}

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

async function loadPage(next = false) {
  if (next ? loadingMore.value : loading.value) return
  if (next && !cursor.value) return
  if (next) loadingMore.value = true
  else {
    loading.value = true
    error.value = ''
  }
  try {
    const page = await listTaskHistory(next ? cursor.value : null)
    const known = new Set(historyTasks.value.map((task) => task.id))
    historyTasks.value = next
      ? [...historyTasks.value, ...page.items.filter((task) => !known.has(task.id))]
      : page.items
    cursor.value = page.next_cursor
    loaded.value = true
  } catch (cause: any) {
    error.value = cause?.message || '任务历史暂时无法读取，请检查连接后重试。'
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

async function runSelectedGroupControl(action: 'pause' | 'resume' | 'cancel') {
  if (!selectedGroup.value || !selectedActiveTasks.value.length || controllingCategory.value) return
  const { categoryId, projectId } = selectedGroup.value
  controllingCategory.value = categoryId
  controlError.value = ''
  try {
    await taskStore.controlCategory(projectId, categoryId, action)
  } catch (cause: any) {
    const actionLabel = action === 'pause' ? '暂停' : action === 'resume' ? '启动' : '取消'
    controlError.value = cause?.message || `批量${actionLabel}任务失败，请重试。`
  } finally {
    controllingCategory.value = null
  }
}

function openGroup(categoryId: TaskCenterCategoryId, projectId: string, event: MouseEvent) {
  restoreFocus = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  selectedGroup.value = { categoryId, projectId }
  taskFilter.value = 'all'
  void nextTick(() => dialogCloseButton.value?.focus())
}

function closeDialog(returnFocus = true) {
  selectedGroup.value = null
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

async function activate() {
  taskStore.setTaskCenterOpen(true)
  if (!loaded.value) {
    await loadPage()
    // keep pulling earlier pages (capped) so a large recent batch cannot push
    // completed entries out of the auto-loaded view
    for (let i = 0; i < 20 && cursor.value && historyTasks.value.length < AUTO_HISTORY_LIMIT; i += 1) {
      await loadPage(true)
    }
  }
  void taskStore.refresh()
}

onActivated(activate)
onDeactivated(() => {
  closeDialog(false)
  taskStore.setTaskCenterOpen(false)
})
</script>

<template>
  <div class="task-center">
    <header class="task-center__header">
      <div>
        <p class="eyebrow">CROSS-PROJECT OVERVIEW</p>
        <h1 class="page-title">任务中心</h1>
        <p class="task-center__subtitle">集中查看各本书的制作任务和实时进度。</p>
      </div>
      <Button variant="outline" :disabled="loading" @click="loadPage()">
        <RefreshCw class="h-4 w-4" :class="loading ? 'animate-spin' : ''" />刷新历史
      </Button>
    </header>

    <div v-if="error" class="task-center__error" role="alert">
      <CircleAlert class="h-4 w-4 shrink-0" /><span>{{ error }}</span>
      <Button variant="outline" size="sm" @click="loadPage()">重试</Button>
    </div>

    <div v-if="controlError" class="task-center__error" role="alert">
      <CircleAlert class="h-4 w-4 shrink-0" /><span>{{ controlError }}</span>
      <Button variant="ghost" size="sm" aria-label="关闭批量控制错误提示" @click="controlError = ''"><X class="h-4 w-4" /></Button>
    </div>

    <div v-if="loading && !loaded" class="task-center__loading" role="status">
      <LoaderCircle class="h-5 w-5 animate-spin" />正在读取任务记录…
    </div>

    <div v-else class="task-center__sections">
      <Card v-for="(section, index) in sections" :key="section.id" class="task-center__section">
        <div class="task-center__section-head">
          <div class="task-center__section-icon" aria-hidden="true">
            <component :is="[ScanText, Users, Users, AudioLines, Combine, Music4][index]" class="h-4 w-4" />
          </div>
          <div class="task-center__section-copy">
            <h2>{{ section.label }}</h2>
            <p>{{ section.description }}</p>
          </div>
          <span class="task-center__section-count">{{ section.taskCount }} 项任务</span>
        </div>

        <div v-if="section.groups.length" class="task-center__folder-list">
          <button
            v-for="group in section.groups"
            :key="group.projectId"
            type="button"
            class="task-center__folder"
            :aria-label="`${group.projectName}，${group.tasks.length} 项任务，打开子任务`"
            @click="openGroup(section.id, group.projectId, $event)"
          >
            <span class="task-center__folder-icon" aria-hidden="true"><Folder class="h-5 w-5" /></span>
            <span class="task-center__folder-copy">
              <strong>{{ group.projectName }}</strong>
              <small>{{ group.tasks.length }} 项任务<span v-if="group.activeCount"> · {{ group.activeCount }} 项进行中</span></small>
            </span>
            <StatusPill v-if="group.activeCount" :label="`${group.activeCount} 项进行中`" tone="warning" />
            <StatusPill v-else :label="statusInfo(group.tasks[0].status).label" :tone="statusInfo(group.tasks[0].status).tone" />
            <ChevronRight class="task-center__folder-arrow h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <div v-else class="task-center__empty-section">
          <span>此分区还没有任务</span>
        </div>
      </Card>
    </div>

    <div v-if="cursor" class="task-center__more">
      <Button variant="outline" :disabled="loadingMore" @click="loadPage(true)">
        <LoaderCircle v-if="loadingMore" class="h-4 w-4 animate-spin" />
        <span v-else>加载更早的任务</span>
      </Button>
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
            <p>{{ selectedTasks.length }} 项子任务</p>
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
                :disabled="!selectedPausableTasks.length || !!controllingCategory"
                :aria-busy="controllingCategory === selectedGroup.categoryId"
                :aria-label="`${selectedProjectName}的${selectedCategory.label}：暂停全部任务`"
                @click="runSelectedGroupControl('pause')"
              >
                <LoaderCircle v-if="controllingCategory === selectedGroup.categoryId" class="h-4 w-4 animate-spin" />
                <Pause v-else class="h-4 w-4" />
                暂停全部
              </Button>
              <Button
                variant="outline"
                size="sm"
                class="task-center__batch-control"
                :disabled="!selectedResumableTasks.length || !!controllingCategory"
                :aria-busy="controllingCategory === selectedGroup.categoryId"
                :aria-label="`${selectedProjectName}的${selectedCategory.label}：启动全部任务`"
                @click="runSelectedGroupControl('resume')"
              >
                <LoaderCircle v-if="controllingCategory === selectedGroup.categoryId" class="h-4 w-4 animate-spin" />
                <Play v-else class="h-4 w-4" />
                启动全部
              </Button>
              <Button
                variant="outline"
                size="sm"
                class="task-center__batch-control text-destructive"
                :disabled="!selectedActiveTasks.length || !!controllingCategory"
                :aria-label="`${selectedProjectName}的${selectedCategory.label}：取消此批全部任务`"
                @click="runSelectedGroupControl('cancel')"
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
                @click="taskFilter = filter.id"
              >{{ filter.label }}</button>
            </div>
          </div>
        </header>

        <div v-if="filteredSelectedTasks.length" class="task-center__task-list">
          <article v-for="task in filteredSelectedTasks" :key="task.id" class="task-center__task-row">
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
          <p>{{ selectedTasks.length ? '当前筛选下没有子任务。' : '此书籍在该分区还没有子任务。' }}</p>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.task-center{display:grid;gap:18px;max-width:1320px;margin:0 auto;padding-bottom:30px}.task-center__header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}.task-center__header .page-title{margin-top:3px}.task-center__subtitle{margin-top:5px;color:hsl(var(--muted-foreground));font-size:12px}.task-center__sections{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.task-center__section{min-width:0;padding:15px}.task-center__section-head{display:flex;align-items:center;gap:10px}.task-center__section-icon{display:grid;place-items:center;width:34px;height:34px;flex:none;border:1px solid hsl(var(--primary)/.12);border-radius:10px;background:hsl(var(--primary)/.08);color:hsl(var(--primary))}.task-center__section-copy{min-width:0;flex:1}.task-center__section-copy h2{font-size:13px;font-weight:750;line-height:1.4}.task-center__section-copy p{margin-top:2px;color:hsl(var(--muted-foreground));font-size:10px}.task-center__section-count{flex:none;color:hsl(var(--muted-foreground));font-size:10px;font-variant-numeric:tabular-nums}.task-center__folder-list{display:grid;gap:6px;margin-top:13px}.task-center__folder{display:flex;align-items:center;gap:10px;width:100%;min-height:60px;border:1px solid hsl(var(--border)/.75);border-radius:11px;background:var(--glass-tint);padding:9px 10px;text-align:left;transition:border-color .15s,background .15s,transform .15s}.task-center__folder:hover{transform:translateY(-1px);border-color:hsl(var(--primary)/.35);background:hsl(var(--primary)/.04)}.task-center__folder:focus-visible,.task-center__close:focus-visible{outline:2px solid hsl(var(--ring));outline-offset:2px}.task-center__folder-icon{display:grid;place-items:center;width:34px;height:34px;flex:none;border-radius:9px;background:hsl(var(--primary)/.08);color:hsl(var(--primary))}.task-center__folder-copy{display:grid;gap:2px;min-width:0;flex:1}.task-center__folder-copy strong{overflow:hidden;font-size:12px;text-overflow:ellipsis;white-space:nowrap}.task-center__folder-copy small{color:hsl(var(--muted-foreground));font-size:10px}.task-center__folder-arrow{flex:none;color:hsl(var(--muted-foreground))}.task-center__empty-section{display:grid;place-items:center;min-height:74px;margin-top:11px;border:1px dashed hsl(var(--border));border-radius:10px;color:hsl(var(--muted-foreground));font-size:11px}.task-center__loading{display:flex;align-items:center;justify-content:center;gap:9px;min-height:220px;color:hsl(var(--muted-foreground));font-size:13px}.task-center__error{display:flex;align-items:center;gap:9px;border:1px solid hsl(var(--destructive)/.2);border-radius:11px;background:hsl(var(--destructive)/.05);padding:10px 12px;color:hsl(var(--destructive));font-size:12px}.task-center__error span{flex:1}.task-center__more{display:flex;justify-content:center}.task-center__overlay{position:fixed;inset:0;z-index:80;display:grid;place-items:center;overflow:auto;background:rgba(18,20,37,.48);padding:22px}.task-center__dialog{display:flex;flex-direction:column;width:min(760px,100%);height:min(82vh,820px);overflow:hidden;border:1px solid var(--glass-border);border-radius:18px;background:var(--glass-tint-strong);box-shadow:var(--glass-highlight),var(--glass-shadow-strong);backdrop-filter:blur(var(--glass-blur))}.task-center__dialog-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;border-bottom:1px solid hsl(var(--border)/.75);padding:20px 22px}.task-center__dialog-head h2{margin-top:3px;font-size:20px;font-weight:760;overflow-wrap:anywhere}.task-center__dialog-head p:last-child{margin-top:3px;color:hsl(var(--muted-foreground));font-size:11px}.task-center__close{display:grid;place-items:center;width:40px;height:40px;flex:none;border:1px solid hsl(var(--border));border-radius:10px;background:hsl(var(--background)/.65);color:hsl(var(--muted-foreground));transition:color .14s,border-color .14s}.task-center__close:hover{border-color:hsl(var(--primary)/.35);color:hsl(var(--foreground))}.task-center__task-list{flex:1;display:grid;gap:0;min-height:0;overflow:auto;padding:0 22px}.task-center__task-row{display:flex;gap:11px;border-bottom:1px solid hsl(var(--border)/.72);padding:15px 0}.task-center__task-row:last-child{border-bottom:0}.task-center__task-icon{display:grid;place-items:center;width:32px;height:32px;flex:none;border-radius:9px;background:hsl(var(--primary)/.08);color:hsl(var(--primary))}.task-center__task-main{min-width:0;flex:1}.task-center__task-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.task-center__task-title{display:grid;gap:2px;min-width:0}.task-center__task-title strong{font-size:12px;overflow-wrap:anywhere}.task-center__task-title span{color:hsl(var(--muted-foreground));font-size:10px}.task-center__task-progress{display:flex;align-items:center;gap:9px;margin-top:9px}.task-center__task-progress>div{flex:1}.task-center__task-progress>span{width:36px;color:hsl(var(--muted-foreground));font-size:10px;text-align:right;font-variant-numeric:tabular-nums}.task-center__task-current,.task-center__task-error{display:flex;align-items:flex-start;gap:5px;margin-top:7px;color:hsl(var(--muted-foreground));font-size:10px;overflow-wrap:anywhere}.task-center__task-current svg{flex:none;margin-top:1px;color:hsl(var(--primary))}.task-center__task-error{color:hsl(var(--destructive))}.task-center__dialog-empty{display:grid;place-items:center;align-content:center;gap:8px;flex:1;min-height:0;color:hsl(var(--muted-foreground));font-size:12px}.task-center__dialog-empty svg{color:hsl(var(--primary))}@supports not (backdrop-filter:blur(1px)){.task-center__dialog{background:hsl(var(--background))}}@media(max-width:900px){.task-center__sections{grid-template-columns:1fr}}@media(max-width:520px){.task-center__header{align-items:flex-start}.task-center__header>button{width:100%}.task-center__section{padding:12px}.task-center__section-head{gap:8px}.task-center__section-copy h2{font-size:12px}.task-center__section-count{font-size:9px}.task-center__overlay{padding:10px}.task-center__dialog{height:90vh;border-radius:14px}.task-center__dialog-head{padding:16px}.task-center__task-list{padding:0 16px}.task-center__task-heading{align-items:flex-start}.task-center__task-heading .status-pill{flex:none}}
/* Keep each task readable as a single horizontal entry. Narrow screens can scroll the row. */
.task-center__dialog{width:min(1120px,100%)}
.task-center__task-list{overflow:auto;padding:0 18px}
/* 标题/详情/状态列取固定宽度（内容过长省略号截断，hover 可见全文），保证各行
 * 时间、进度条、详情、状态列纵向对齐，且行宽不足压缩时不因行内容宽度不同
 * 产生错位；进度条列 1fr 吃满剩余空间。状态胶囊最宽为 3 个汉字（68px）。 */
.task-center__task-row{display:grid;grid-template-columns:32px minmax(0,380px) max-content minmax(180px,1fr) minmax(0,150px) 70px;align-items:center;gap:12px;width:100%;min-width:900px;padding:10px 0}
.task-center__task-icon{width:30px;height:30px}
.task-center__task-title{min-width:0;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
</style>
