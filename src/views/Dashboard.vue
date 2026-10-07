<script setup lang="ts">
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { BookOpen, Clock3, FolderPlus, LoaderCircle, Plus, RefreshCw, Trash2 } from 'lucide-vue-next'
import Pager from '@/views/textformat/Pager.vue'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import { useProjectStore } from '@/stores/project'
import { useSettingsStore } from '@/stores/settings'
import { taskTypeLabel } from '@/utils/taskLabels'
import { listProjectPage, deleteProject, getActiveProject } from '@/api/project'
import type { ProjectSummary } from '@/api/project'
import { useAuthStore } from '@/stores/auth'
import { useProjectCardProgress } from '@/composables/useProjectCardProgress'
import { stageProgressColor } from '@/utils/projectStageProgress'

const router = useRouter()
const auth = useAuthStore()
let viewActive = false
const projectStore = useProjectStore()
const settings = useSettingsStore()
const { push: toast } = useToast()
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
const stageLabels: Record<string, string> = {
  '02_split_text': '分册', '03_parsed_json': '解析', '04_voice_profiles': '配音',
  '05_audio_chunk': '合成', '06_audio_merge': '合并', '07_output': '分割', '08_bgm': '混音',
}
function progressFor(projectId: string, key: string) {
  return summaries.value[projectId]?.stage_completion?.[key]
}
function progressTitle(projectId: string, key: string) {
  const value = progressFor(projectId, key)
  if (!value) return `${stageLabels[key]}：正在读取进度`
  if (value.percent === null) return `${stageLabels[key]}：总量尚未确定，需先完成上游解析或分集计划`
  return `${stageLabels[key]}：${value.percent}%；已完成 ${value.completed} / ${value.total} ${value.unit}`
}
const projectPage = ref(1)
const projectTotal = ref(0)
const pageProjects = ref<ProjectSummary[] | null>(null)
const projects = computed(() => pageProjects.value ?? [])
const { summaries, tasks, errors: progressErrors, refresh: refreshSummaries } = useProjectCardProgress(computed(() => projects.value.map(item => item.id)))
const progressLabels: Record<string, string> = { tasks: '任务状态', text: '分册', catalog: '解析与配音', production: '音频制作' }
function cardError(id: string) {
  return Object.entries(progressErrors.value[id] ?? {}).map(([section, message]) => `${progressLabels[section]}暂未更新：${message}`).join('；')
}
watch(projectPage, () => { pageProjects.value = null; loading.value = true; if (viewActive && auth.user) void loadProjectPage() })
let projectPageRequest = 0
let projectPageAbort: AbortController | null = null
async function loadProjectPage() {
  projectPageAbort?.abort()
  projectPageAbort = new AbortController()
  const signal = projectPageAbort.signal
  const request = ++projectPageRequest
  const generation = loadGeneration
  try {
    const response = await listProjectPage({ page: projectPage.value, page_size: 12 }, signal)
    if (signal.aborted || request !== projectPageRequest || generation !== loadGeneration) return
    pageProjects.value = response.items; projectTotal.value = response.pagination.total
    const last = Math.max(1, Math.ceil(projectTotal.value / 12))
    if (projectPage.value > last) { projectPage.value = last; return }
    pageError.value = ''
    loading.value = false
  } catch (e: any) { if (!signal.aborted && request === projectPageRequest && generation === loadGeneration) { pageError.value = e?.message || '项目读取失败'; loading.value = false } }
}


function projectState(project: ProjectSummary) {
  const summary = tasks.value[project.id]
  const status = summary?.statuses[0]
  if (!status) return null
  return { ...status, error_message: summary.failures.find(item => item.task_type === status.task_type)?.error_message }
}

function statusLabel(status: string) {
  return ({ pending: '等待中', queued: '排队中', running: '处理中', retrying: '重试中',
    paused: '已暂停', succeeded: '已完成', failed: '失败', timeout: '超时', cancelled: '已取消', cancelling: '正在取消' } as Record<string, string>)[status] || status
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

async function syncActiveProject() {
  const userId = auth.user?.id
  const previous = projectStore.current
  const current = await getActiveProject()
  // A new login or project selection owns the shared context once it starts.
  if (userId === auth.user?.id && previous === projectStore.current && !projectStore.busy) {
    projectStore.setCurrent(current)
  }
}

async function load(force = false) {
  if (!viewActive || !auth.user || document.hidden) return
  const requestGeneration = ++loadGeneration
  refreshing.value = true
  pageError.value = ''
  const previousIds = JSON.stringify(projects.value.map(item => item.id))
  await loadProjectPage()
  if (requestGeneration !== loadGeneration) return
  if (force) {
    try { await syncActiveProject() }
    catch { if (requestGeneration === loadGeneration) pageError.value = '当前项目状态暂未同步，请重试。' }
  }
  if (requestGeneration === loadGeneration) {
    refreshing.value = false
    if (force && previousIds === JSON.stringify(projects.value.map(item => item.id))) refreshSummaries()
  }
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
    await load(true)
  } catch (cause: any) {
    toast({ title: '删除项目失败', variant: 'destructive', description: cause?.message || '请稍后重试。' })
  } finally {
    deletingProjectId.value = ''
  }
}

function activate() {
  if (viewActive) return
  viewActive = true
  void load()
}
function visibility() {
  if (document.hidden) { projectPageAbort?.abort(); loadGeneration++ }
  else if (viewActive && auth.user) void load()
}
onMounted(() => { document.addEventListener('visibilitychange', visibility); activate() })
onActivated(activate)
function deactivate() {
  viewActive = false
  projectPageAbort?.abort()
  loadGeneration += 1
}
onDeactivated(deactivate)
onUnmounted(() => { deactivate(); document.removeEventListener('visibilitychange', visibility) })
watch(() => auth.user?.id, () => {
  loadGeneration++
  projectPageAbort?.abort()
  pageProjects.value = null
  projectTotal.value = 0
  pageError.value = ''; openError.value = ''; name.value = ''; createOpen.value = false
  projectPage.value = 1
  loading.value = true
  if (viewActive && auth.user) void load()
})
</script>

<template>
  <div class="project-center viewport-page">
    <header class="page-header">
      <div class="project-center__header">
        <div>
          <p class="eyebrow">NARRIFY AUDIO WORKSPACE</p>
          <h1 class="page-title">我的项目</h1>
          <p class="page-description">继续制作，或从一本新书开始。</p>
        </div>
        <div class="project-center__actions">
          <Button @click="createOpen = !createOpen"><FolderPlus class="h-4 w-4" />新建项目</Button>
        </div>
      </div>
    </header>

    <div class="page-region" role="region" aria-label="页面工作区" tabindex="0">
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
        <span>{{ pageError }}</span><Button variant="outline" size="sm" :disabled="refreshing" @click="load(true)"><RefreshCw class="h-4 w-4" />重试</Button>
      </div>

      <section class="project-center__section">
        <div class="section-heading">
          <div><h2>项目列表</h2><span v-if="!loading" class="muted">{{ projectTotal || projects.length }} 个项目</span></div>
          <Button variant="ghost" size="sm" :disabled="refreshing" @click="load(true)"><RefreshCw class="h-4 w-4" />刷新</Button>
        </div>

        <div v-if="loading" class="project-grid" aria-label="正在加载项目">
          <Card v-for="n in 3" :key="n" class="project-skeleton"><div class="skeleton-line w-2/5" /><div class="skeleton-line w-4/5" /><div class="skeleton-line w-1/2" /></Card>
        </div>
        <div v-else-if="projects.length" class="project-grid">
          <Card v-for="project in projects" :key="project.id" class="project-card" :class="projectStore.activeProjectId === project.id ? 'project-card--active' : ''" @click="!projectStore.busy && !deletingProjectId && openProject(project)">
            <div class="project-card__head">
              <button class="project-card__main" type="button" :disabled="projectStore.busy || !!deletingProjectId" @click.stop="openProject(project)">
                <div class="project-card__icon"><BookOpen class="h-5 w-5" /></div>
                <div class="project-card__copy">
                  <div class="project-card__title"><h3 :title="project.name">{{ project.name }}</h3><StatusPill v-if="projectStore.activeProjectId === project.id" label="当前项目" tone="positive" /></div>
                  <p><Clock3 class="h-3.5 w-3.5" />{{ updatedAt(project.updated_at) }}</p>
                </div>
              </button>
              <div class="project-card__actions">
              <Button variant="ghost" size="icon" class="project-card__delete" :disabled="projectStore.busy || !!deletingProjectId" :aria-label="`删除项目 ${project.name}`" title="删除项目" @click.stop="requestDelete(project)">
                <LoaderCircle v-if="deletingProjectId === project.id" class="h-4 w-4 animate-spin" />
                <Trash2 v-else class="h-4 w-4" />
              </Button>
              </div>
            </div>
            <div class="project-card__meta">
              <div v-if="projectState(project)" class="project-card__task">
                <StatusPill :label="statusLabel(projectState(project)!.status)" :tone="tone(projectState(project)!.status)" />
                <span>{{ taskTypeLabel(projectState(project)!.task_type) }}</span>
              </div>
                <div class="progress-label"><span>制作进度</span></div>
                <div class="project-stage-track" aria-label="各阶段制作完成进度">
                  <div v-for="key in visibleStageKeys" :key="key" class="project-stage" :class="{ 'progress-unknown': progressFor(project.id, key)?.percent == null }" :style="{ '--progress-color': stageProgressColor(progressFor(project.id, key)?.percent ?? 0) }" :title="progressTitle(project.id, key)">
                    <div class="stage-progress-bar" role="progressbar" :aria-label="`${stageLabels[key]}制作进度`" :aria-valuemin="0" :aria-valuemax="100" :aria-valuenow="progressFor(project.id, key)?.percent ?? undefined" :aria-valuetext="progressTitle(project.id, key)">
                      <span :style="{ width: `${progressFor(project.id, key)?.percent ?? 0}%` }" />
                    </div>
                    <div class="stage-progress-label"><span>{{ stageLabels[key] }}</span><strong>{{ progressFor(project.id, key)?.percent != null ? `${progressFor(project.id, key)!.percent}%` : '—' }}</strong></div>
                  </div>
                </div>
              <div v-if="cardError(project.id)" class="project-error" role="alert" @click.stop>
                {{ cardError(project.id) }} <button type="button" class="underline" @click="refreshSummaries(project.id)">重试</button>
              </div>
            </div>
            <p v-if="projectState(project)?.status === 'failed'" class="project-card__error">{{ projectState(project)?.error_message || '最近任务失败，可进入项目查看详情。' }}</p>
          </Card>
        </div>
        <div v-else-if="!pageError" class="project-empty">
          <div class="project-empty__icon"><BookOpen class="h-6 w-6" /></div>
          <h3>还没有项目</h3>
          <p>创建项目后导入原文，制作进度和生成内容都会归在这里。</p>
          <Button @click="createOpen = true"><FolderPlus class="h-4 w-4" />创建第一个项目</Button>
        </div>
        <Pager :page="projectPage" :page-count="Math.max(1, Math.ceil(projectTotal / 12))" :total="projectTotal" :page-size="12" unit="个项目" @update:page="projectPage = $event" />
      </section>
    </div>

  </div>
</template>

<style scoped>
.project-center{display:grid;gap:26px;max-width:1320px;margin:0 auto;padding-bottom:30px}.project-center__header{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.16em;color:hsl(var(--primary))}.project-center__actions{display:flex;gap:9px;flex-wrap:wrap}.section-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px}.section-heading>div{display:flex;align-items:baseline;gap:10px}.section-heading h2{font-size:16px;font-weight:750}.muted,.empty-inline{color:hsl(var(--muted-foreground));font-size:12px}.project-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.project-card{overflow:hidden;border-color:hsl(var(--border));transition:border-color .15s,box-shadow .15s}.project-card--active{border-color:hsl(var(--primary)/.38);box-shadow:0 0 0 1px hsl(var(--primary)/.08)}.project-card__head{display:flex;align-items:center;gap:2px;padding-right:8px}.project-card__main{display:flex;width:auto;min-width:0;flex:1;align-items:center;gap:12px;padding:16px 16px 12px;text-align:left}.project-card__main:hover .project-card__arrow{transform:translateX(3px);color:hsl(var(--primary))}.project-card__main:disabled{opacity:.55}.project-card__delete{flex:none;color:hsl(var(--muted-foreground))}.project-card__delete:hover{color:hsl(var(--destructive))}.project-card__icon,.project-empty__icon{display:grid;place-items:center;width:42px;height:42px;flex:none;border-radius:12px;background:hsl(var(--primary)/.09);color:hsl(var(--primary))}.project-card__copy{min-width:0;flex:1}.project-card__title{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.project-card__title h3{overflow:hidden;font-size:14px;font-weight:700;text-overflow:ellipsis;white-space:nowrap}.project-card__copy p{display:flex;align-items:center;gap:5px;margin-top:6px;color:hsl(var(--muted-foreground));font-size:11px}.project-card__arrow{flex:none;color:hsl(var(--muted-foreground));transition:transform .15s}.project-card__meta{min-height:49px;margin:0 16px;padding:11px 0 13px;border-top:1px solid hsl(var(--border))}.progress-label{display:flex;justify-content:space-between;gap:12px;font-size:11px;color:hsl(var(--muted-foreground))}.progress-label strong{color:hsl(var(--foreground));font-weight:650}.progress-track{height:5px;margin-top:8px;overflow:hidden;border-radius:999px;background:hsl(var(--muted))}.progress-track span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,hsl(var(--primary)),hsl(199 89% 48%));transition:width .2s}.project-card__task{display:flex;align-items:center;gap:8px}.project-card__task>span{overflow:hidden;color:hsl(var(--muted-foreground));font-size:11px;text-overflow:ellipsis;white-space:nowrap}.project-card__error,.project-error{margin:0 16px 13px;color:hsl(var(--destructive));font-size:12px;overflow-wrap:anywhere}.project-create{padding:16px}.project-create__form{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.project-create__form label{font-size:13px;font-weight:650}.project-create__form input{height:38px;min-width:210px;flex:1;border:1px solid hsl(var(--input));border-radius:8px;background:hsl(var(--background));padding:0 11px;font-size:13px}.project-error{margin:10px 0 0}.project-alert{display:flex;justify-content:space-between;align-items:center;gap:14px;border:1px solid hsl(var(--destructive)/.25);border-radius:10px;background:hsl(var(--destructive)/.06);padding:10px 12px;color:hsl(var(--destructive));font-size:12px}.project-empty{display:grid;justify-items:center;gap:10px;border:1px dashed hsl(var(--border));border-radius:14px;padding:44px 18px;text-align:center}.project-empty__icon{width:50px;height:50px}.project-empty h3{font-size:16px;font-weight:700}.project-empty p{max-width:460px;color:hsl(var(--muted-foreground));font-size:12px}.project-skeleton{display:grid;gap:13px;padding:18px}.skeleton-line{height:11px;border-radius:6px;background:hsl(var(--muted));animation:pulse 1.4s ease-in-out infinite}.project-skeleton .w-2\/5{width:40%}.project-skeleton .w-4\/5{width:80%}.project-skeleton .w-1\/2{width:50%}@keyframes pulse{50%{opacity:.4}}@media(max-width:750px){.project-grid{grid-template-columns:1fr}.project-center__header{align-items:flex-start}.project-center__actions{width:100%}}
.project-center { width:100%; min-width:0; }
.project-grid { grid-template-columns:minmax(0,1fr); gap:0; border:1px solid hsl(var(--border)); border-radius:12px; overflow:hidden; background:hsl(var(--card)); }
.project-card { display:grid; grid-template-columns:minmax(0,1fr) minmax(260px,34%) 52px; align-items:center; border:0; border-bottom:1px solid hsl(var(--border)); border-radius:0; box-shadow:none; background:transparent; cursor:pointer; }
.project-card:hover { background:hsl(var(--primary)/.04); }
.project-card:has(.project-card__main:focus-visible) { outline:2px solid hsl(var(--ring)); outline-offset:-2px; }
.project-card:last-child { border-bottom:0; }
.project-card--active { background:hsl(var(--primary)/.035); box-shadow:inset 3px 0 hsl(var(--primary)); }
.project-card__head { display:contents; }
.project-card__main { grid-column:1; grid-row:1; min-height:100px; padding:20px; }
.project-card__actions { grid-column:3; grid-row:1; display:flex; align-items:center; justify-content:flex-end; padding-right:12px; }
.project-card__icon { width:38px; height:46px; border-radius:6px; border:1px solid hsl(var(--primary)/.12); }
.project-card__title { flex-wrap:nowrap; }
.project-card__title h3 { min-width:0; font-size:15px; font-weight:650; }
.project-card__title :deep([role="status"]) { flex-shrink:0; }
.project-card__meta { grid-column:2; grid-row:1; min-width:0; margin:0; padding:20px 16px; border:0; }
.project-stage-track { display:grid; grid-auto-flow:column; grid-auto-columns:minmax(0,1fr); gap:5px; margin-top:12px; }
.project-stage { min-width:0; }
.stage-progress-bar { height:5px; overflow:hidden; border-radius:3px; background:rgb(var(--progress-color)/.16); transition:background-color .45s; }
.stage-progress-bar>span { display:block; min-width:2px; height:100%; border-radius:inherit; background:rgb(var(--progress-color)); transition:width .45s ease,background-color .45s ease; }
.progress-unknown .stage-progress-bar { background:hsl(var(--muted)); }
.progress-unknown .stage-progress-bar>span { min-width:0; }
.stage-progress-label { display:flex; flex-direction:column; align-items:center; gap:2px; margin-top:6px; font-size:10px; color:hsl(var(--muted-foreground)); }
.stage-progress-label strong { font-size:9px; font-weight:500; font-variant-numeric:tabular-nums; color:hsl(var(--foreground)); }
.project-card__task { margin-bottom:8px; }
@media(prefers-reduced-motion:reduce) { .stage-progress-bar,.stage-progress-bar>span { transition:none; } }
.project-card__error { grid-column:1/-1; }
.project-card__task { flex-wrap:wrap; }
.project-card__task>span { white-space:normal; overflow-wrap:anywhere; }
@media(max-width:900px) {
  .project-card { grid-template-columns:minmax(0,1fr) 52px; }
  .project-card__actions { grid-column:2; }
  .project-card__main { min-height:78px; padding:16px; }
  .project-card__meta { grid-column:1/-1; grid-row:2; padding:0 16px 16px 66px; }
}
@media(prefers-reduced-motion:reduce) { .project-card, .project-card__arrow, .skeleton-line { transition:none; animation:none; } }
</style>
