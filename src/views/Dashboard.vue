<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, BookOpen, Clock3, FolderPlus, LoaderCircle, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import { useProjectStore } from '@/stores/project'
import { useSettingsStore } from '@/stores/settings'
import { taskTypeLabel } from '@/utils/taskLabels'
import { listDurableTasks, type DurableTask } from '@/api/durableTasks'
import { deleteProject, getProjectSummary, type ProjectStorageSummary } from '@/api/project'
import type { ProjectSummary } from '@/api/project'

const router = useRouter()
const projectStore = useProjectStore()
const settings = useSettingsStore()
const { push: toast } = useToast()
const tasks = ref<DurableTask[]>([])
const summaries = ref<Record<string, ProjectStorageSummary>>({})
const loading = ref(true)
const refreshing = ref(false)
const createOpen = ref(false)
const name = ref('')
const pageError = ref('')
const openError = ref('')
const deletingProjectId = ref('')
let loadGeneration = 0
const STAGE_KEYS = ['02_split_text', '03_parsed_json', '04_voice_profiles', '05_audio_chunk', '06_audio_merge', '07_output', '08_bgm']
const visibleStageKeys = computed(() => STAGE_KEYS.filter((key) => key !== '07_output' || settings.config?.ui.show_audio_split))
const stageCount = computed(() => visibleStageKeys.value.length)

const projects = computed(() => [...projectStore.projects].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at)))

function stagesWithOutput(summary?: ProjectStorageSummary) {
  if (!summary) return null
  const completed = new Set(summary.categories.filter((row) => row.count > 0).map((row) => row.key))
  return visibleStageKeys.value.filter((key) =>
    key === '02_split_text' ? summary.split_volume_count > 0 : completed.has(key),
  ).length
}

function projectState(project: ProjectSummary) {
  const task = tasks.value.find((item) => item.project_id === project.id && !['succeeded', 'cancelled'].includes(item.status))
  if (!task) return null
  return task
}

function statusLabel(status: string) {
  return ({ pending: '等待中', queued: '排队中', running: '处理中', retrying: '重试中',
    succeeded: '已完成', failed: '失败', timeout: '超时', cancelled: '已取消', cancelling: '正在取消' } as Record<string, string>)[status] || status
}

function tone(status: string): 'positive' | 'warning' | 'negative' | 'neutral' {
  if (status === 'succeeded') return 'positive'
  if (['failed', 'timeout'].includes(status)) return 'negative'
  if (['running', 'queued', 'pending', 'retrying', 'cancelling'].includes(status)) return 'warning'
  return 'neutral'
}

function updatedAt(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '最近更新未知' : `最近更新 ${date.toLocaleDateString()}`
}

async function load() {
  const requestGeneration = ++loadGeneration
  refreshing.value = true
  pageError.value = ''
  const [active, tasksResult] = await Promise.all([
    projectStore.refresh(),
    Promise.allSettled([listDurableTasks()]),
  ])
  if (requestGeneration !== loadGeneration) return
  tasks.value = tasksResult[0].status === 'fulfilled' ? tasksResult[0].value : []
  if (projectStore.error) pageError.value = projectStore.error
  else if (tasksResult[0].status === 'rejected') pageError.value = '最近任务暂时无法读取。'
  const visible = projects.value.slice(0, 12)
  loading.value = false
  refreshing.value = false
  const results = await Promise.allSettled(visible.map((project) => getProjectSummary(project.id, true)))
  if (requestGeneration !== loadGeneration) return
  const next: Record<string, ProjectStorageSummary> = {}
  results.forEach((result, index) => {
    if (result.status === 'fulfilled') next[visible[index].id] = result.value
  })
  summaries.value = next
  if (active?.set && !projectStore.projects.some((item) => item.id === active.project_id)) {
    await projectStore.refresh()
  }
  refreshing.value = false
  loading.value = false
}

async function createProject() {
  const projectName = name.value.trim()
  if (!projectName || projectStore.busy) return
  openError.value = ''
  try {
    const project = await projectStore.create(projectName)
    await router.push(`/projects/${project.id}`)
  } catch (cause: any) {
    openError.value = cause?.message || '创建项目失败，请稍后重试。'
  }
}

async function openProject(project: ProjectSummary) {
  if (projectStore.busy) return
  openError.value = ''
  try {
    if (projectStore.activeProjectId !== project.id) await projectStore.select(project.id)
    await router.push(`/projects/${project.id}`)
  } catch (cause: any) {
    openError.value = cause?.message || '无法打开此项目。'
  }
}

// Shared confirm dialog (Q21): the shared host owns focus return / Esc /
// Tab trap; a failed delete surfaces as a toast instead of an in-dialog
// error, and the list reloads on success.
async function requestDelete(project: ProjectSummary) {
  if (projectStore.busy || deletingProjectId.value) return
  const confirmed = await showConfirm(
    `将项目「${project.name}」移入回收站。本地文件和任务记录会保留一个自然月，可在回收站恢复；到期后会彻底删除。如果项目有未完成任务，需要先等待任务完成或取消任务。`,
    { title: '删除项目', confirmText: '删除项目', destructive: true },
  )
  if (!confirmed) return
  deletingProjectId.value = project.id
  try {
    await deleteProject(project.id)
    await load()
  } catch (cause: any) {
    toast({ title: '删除项目失败', variant: 'destructive', description: cause?.message || '请稍后重试。' })
  } finally {
    deletingProjectId.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="project-center">
    <header class="page-header project-center__header">
      <div>
        <p class="eyebrow">NARRIFY AUDIO WORKSPACE</p>
        <h1 class="page-title">我的项目</h1>
        <p class="page-description">继续制作，或从一本新书开始。</p>
      </div>
      <div class="project-center__actions">
        <Button @click="createOpen = !createOpen"><FolderPlus class="h-4 w-4" />新建项目</Button>
      </div>
    </header>

    <Card v-if="createOpen" class="project-create">
      <form class="project-create__form" @submit.prevent="createProject">
        <label for="project-name">项目名称</label>
        <input id="project-name" v-model="name" autofocus maxlength="160" placeholder="例如：夏日来信" />
        <Button type="submit" :disabled="!name.trim() || projectStore.busy">
          <LoaderCircle v-if="projectStore.busy" class="h-4 w-4 animate-spin" />
          <Plus v-else class="h-4 w-4" />
          {{ projectStore.busy ? '正在创建…' : '创建并进入' }}
        </Button>
      </form>
      <p v-if="openError" class="project-error" role="alert">{{ openError }}</p>
    </Card>

    <div v-if="pageError" class="project-alert" role="alert">
      <span>{{ pageError }}</span><Button variant="outline" size="sm" :disabled="refreshing" @click="load"><RefreshCw class="h-4 w-4" />重试</Button>
    </div>

    <section class="project-center__section">
      <div class="section-heading">
        <div><h2>项目列表</h2><span v-if="!loading" class="muted">{{ projects.length }} 个项目</span></div>
        <Button variant="ghost" size="sm" :disabled="refreshing" @click="load"><RefreshCw class="h-4 w-4" />刷新</Button>
      </div>

      <div v-if="loading" class="project-grid" aria-label="正在加载项目">
        <Card v-for="n in 3" :key="n" class="project-skeleton"><div class="skeleton-line w-2/5" /><div class="skeleton-line w-4/5" /><div class="skeleton-line w-1/2" /></Card>
      </div>
      <div v-else-if="projects.length" class="project-grid">
        <Card v-for="project in projects" :key="project.id" class="project-card" :class="projectStore.activeProjectId === project.id ? 'project-card--active' : ''">
          <div class="project-card__head">
            <button class="project-card__main" type="button" :disabled="projectStore.busy || deletingProjectId === project.id" @click="openProject(project)">
              <div class="project-card__icon"><BookOpen class="h-5 w-5" /></div>
              <div class="project-card__copy">
                <div class="project-card__title"><h3>{{ project.name }}</h3><StatusPill v-if="projectStore.activeProjectId === project.id" label="当前项目" tone="positive" /></div>
                <p><Clock3 class="h-3.5 w-3.5" />{{ updatedAt(project.updated_at) }}</p>
              </div>
              <ArrowRight class="project-card__arrow h-4 w-4" />
            </button>
            <Button variant="ghost" size="icon" class="project-card__delete" :disabled="projectStore.busy || !!deletingProjectId" :aria-label="`删除项目 ${project.name}`" title="删除项目" @click="requestDelete(project)">
              <LoaderCircle v-if="deletingProjectId === project.id" class="h-4 w-4 animate-spin" />
              <Trash2 v-else class="h-4 w-4" />
            </Button>
          </div>
          <div class="project-card__meta">
            <div v-if="projectState(project)" class="project-card__task">
              <StatusPill :label="statusLabel(projectState(project)!.status)" :tone="tone(projectState(project)!.status)" />
              <span>{{ projectState(project)!.current || taskTypeLabel(projectState(project)!.task_type) }}</span>
            </div>
            <template v-else-if="stagesWithOutput(summaries[project.id]) !== null">
              <div class="progress-label"><span>制作进度</span><strong>{{ stagesWithOutput(summaries[project.id]) }} / {{ stageCount }} 阶段已有产物</strong></div>
              <div class="progress-track"><span :style="{ width: `${(stagesWithOutput(summaries[project.id])! / stageCount) * 100}%` }" /></div>
            </template>
            <span v-else class="muted">打开项目查看制作进度</span>
          </div>
          <p v-if="projectState(project)?.status === 'failed'" class="project-card__error">{{ projectState(project)?.error_message || '最近任务失败，可进入项目查看详情。' }}</p>
        </Card>
      </div>
      <div v-else class="project-empty">
        <div class="project-empty__icon"><BookOpen class="h-6 w-6" /></div>
        <h3>还没有项目</h3>
        <p>创建项目后导入原文，制作进度和生成内容都会归在这里。</p>
        <Button @click="createOpen = true"><FolderPlus class="h-4 w-4" />创建第一个项目</Button>
      </div>
    </section>

  </div>
</template>

<style scoped>
.project-center{display:grid;gap:26px;max-width:1320px;margin:0 auto;padding-bottom:30px}.project-center__header{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap}.project-center__header::before{display:none}.project-center__header h1{margin-top:5px}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.16em;color:hsl(var(--primary))}.project-center__actions{display:flex;gap:9px;flex-wrap:wrap}.section-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}.section-heading>div{display:flex;align-items:baseline;gap:10px}.section-heading h2{font-size:16px;font-weight:750}.muted,.empty-inline{color:hsl(var(--muted-foreground));font-size:12px}.project-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.project-card{overflow:hidden;border-color:hsl(var(--border));transition:border-color .15s,box-shadow .15s}.project-card--active{border-color:hsl(var(--primary)/.38);box-shadow:0 0 0 1px hsl(var(--primary)/.08)}.project-card__head{display:flex;align-items:center;gap:2px;padding-right:8px}.project-card__main{display:flex;width:auto;min-width:0;flex:1;align-items:center;gap:12px;padding:16px 16px 12px;text-align:left}.project-card__main:hover .project-card__arrow{transform:translateX(3px);color:hsl(var(--primary))}.project-card__main:disabled{opacity:.55}.project-card__delete{flex:none;color:hsl(var(--muted-foreground))}.project-card__delete:hover{color:hsl(var(--destructive))}.project-card__icon,.project-empty__icon{display:grid;place-items:center;width:42px;height:42px;flex:none;border-radius:12px;background:hsl(var(--primary)/.09);color:hsl(var(--primary))}.project-card__copy{min-width:0;flex:1}.project-card__title{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.project-card__title h3{overflow:hidden;font-size:14px;font-weight:700;text-overflow:ellipsis;white-space:nowrap}.project-card__copy p{display:flex;align-items:center;gap:5px;margin-top:6px;color:hsl(var(--muted-foreground));font-size:11px}.project-card__arrow{flex:none;color:hsl(var(--muted-foreground));transition:transform .15s}.project-card__meta{min-height:49px;margin:0 16px;padding:11px 0 13px;border-top:1px solid hsl(var(--border))}.progress-label{display:flex;justify-content:space-between;gap:12px;font-size:11px;color:hsl(var(--muted-foreground))}.progress-label strong{color:hsl(var(--foreground));font-weight:650}.progress-track{height:5px;margin-top:8px;overflow:hidden;border-radius:999px;background:hsl(var(--muted))}.progress-track span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,hsl(var(--primary)),hsl(199 89% 48%));transition:width .2s}.project-card__task{display:flex;align-items:center;gap:8px}.project-card__task>span{overflow:hidden;color:hsl(var(--muted-foreground));font-size:11px;text-overflow:ellipsis;white-space:nowrap}.project-card__error,.project-error{margin:0 16px 13px;color:hsl(var(--destructive));font-size:12px;overflow-wrap:anywhere}.project-create{padding:16px}.project-create__form{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.project-create__form label{font-size:13px;font-weight:650}.project-create__form input{height:38px;min-width:210px;flex:1;border:1px solid hsl(var(--input));border-radius:8px;background:hsl(var(--background));padding:0 11px;font-size:13px}.project-error{margin:10px 0 0}.project-alert{display:flex;justify-content:space-between;align-items:center;gap:14px;border:1px solid hsl(var(--destructive)/.25);border-radius:10px;background:hsl(var(--destructive)/.06);padding:10px 12px;color:hsl(var(--destructive));font-size:12px}.project-empty{display:grid;justify-items:center;gap:10px;border:1px dashed hsl(var(--border));border-radius:14px;padding:44px 18px;text-align:center}.project-empty__icon{width:50px;height:50px}.project-empty h3{font-size:16px;font-weight:700}.project-empty p{max-width:460px;color:hsl(var(--muted-foreground));font-size:12px}.project-skeleton{display:grid;gap:13px;padding:18px}.skeleton-line{height:11px;border-radius:6px;background:hsl(var(--muted));animation:pulse 1.4s ease-in-out infinite}.project-skeleton .w-2\/5{width:40%}.project-skeleton .w-4\/5{width:80%}.project-skeleton .w-1\/2{width:50%}@keyframes pulse{50%{opacity:.4}}@media(max-width:750px){.project-grid{grid-template-columns:1fr}.project-center__header{align-items:flex-start}.project-center__actions{width:100%}}
</style>
