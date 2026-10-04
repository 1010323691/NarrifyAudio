<script setup lang="ts">
import { computed, onActivated, onDeactivated, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, ArrowRight, AudioLines, CheckCircle2, CircleAlert, FileAudio2, FileText, LoaderCircle, RefreshCw, Upload } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { getProjectProgressSummary, type ProjectProgressSummary } from '@/api/project'
import { listDurableTasks, type DurableTask } from '@/api/durableTasks'
import { useProjectStore } from '@/stores/project'
import { useSettingsStore } from '@/stores/settings'
import { taskTypeLabel } from '@/utils/taskLabels'
import { modulePrefixes } from '@/utils/taskTypes'
import { previewCompletion, stageProgressColor } from '@/utils/projectStageProgress'
import { useTaskStore } from '@/stores/task'

const route = useRoute()
const router = useRouter()
const project = useProjectStore()
const settings = useSettingsStore()
const projectId = computed(() => String(route.params.projectId || ''))
const loading = ref(true)
const error = ref('')
const summary = ref<ProjectProgressSummary | null>(null)
const tasks = ref<DurableTask[]>([])
const refreshing = ref(false)
const taskStore = useTaskStore()
let loadGeneration = 0
let progressTimer: ReturnType<typeof setTimeout> | undefined
let viewActive = false

const STAGE_DEFS = [
  { key: 'text', label: '排版与分册', path: '/text', dir: '02_split_text', icon: FileText, taskTypes: modulePrefixes('text', 'book') },
  { key: 'script', label: '文本解析', path: '/script', dir: '03_parsed_json', icon: FileText, taskTypes: modulePrefixes('script') },
  { key: 'voices', label: '角色配音', path: '/voices', dir: '04_voice_profiles', icon: AudioLines, taskTypes: modulePrefixes('voices') },
  // tts 模块含 tts.reset（重置流），合成阶段只盯在途的 tts.batch
  { key: 'batch', label: '音频合成', path: '/batch', dir: '05_audio_chunk', icon: FileAudio2, taskTypes: ['tts.batch'] },
  { key: 'preview', label: '整章预览', path: '/preview', dir: '05_audio_chunk', icon: FileAudio2, taskTypes: ['tts.preview_render'] },
  { key: 'merge', label: '音频合并', path: '/merge', dir: '06_audio_merge', icon: FileAudio2, taskTypes: modulePrefixes('merge') },
  { key: 'audio', label: '音频分集', path: '/audio', dir: '07_output', icon: AudioLines, taskTypes: modulePrefixes('audio'), optional: true },
  { key: 'bgm', label: '背景音乐', path: '/bgm', dir: '08_bgm', icon: AudioLines, taskTypes: modulePrefixes('bgm') },
]
const STAGES = computed(() => STAGE_DEFS.filter((stage) => !stage.optional || settings.config?.ui.show_audio_split))

function taskFor(stage: typeof STAGE_DEFS[number]) {
  return tasks.value.find((task) => stage.taskTypes.some((prefix) => task.task_type.startsWith(prefix))
    && !['succeeded', 'cancelled'].includes(task.status))
}

function stageStatus(stage: typeof STAGE_DEFS[number]) {
  const task = taskFor(stage)
  if (task && ['failed', 'timeout'].includes(task.status)) return { label: '需处理', tone: 'negative' as const }
  if (task && ['running', 'queued', 'pending', 'retrying', 'paused'].includes(task.status)) {
    if (task.status === 'paused') return { label: '等待 LLM 恢复', tone: 'warning' as const }
    return { label: task.status === 'running' ? '处理中' : '排队中', tone: 'warning' as const }
  }
  const value = completionFor(stage)
  if (value?.percent === 100) return { label: '已完成', tone: 'positive' as const }
  if (value?.percent === null) return { label: '总量待确定', tone: 'neutral' as const }
  if (value && value.completed > 0) return { label: '部分完成', tone: 'warning' as const }
  return { label: '待开始', tone: 'neutral' as const }
}

function completionFor(stage: typeof STAGE_DEFS[number]) {
  const value = summary.value?.stage_completion?.[stage.dir]
  return stage.key === 'preview' && value ? previewCompletion(value) : value
}
const productionStages = computed(() => STAGES.value.filter(stage => stage.key !== 'preview'))
const doneStages = computed(() => STAGES.value.filter(stage => completionFor(stage)?.percent === 100).length)
const unknownStages = computed(() => productionStages.value.filter(stage => completionFor(stage)?.percent == null).length)
const nextStage = computed(() => productionStages.value.find(stage => stageStatus(stage).tone === 'negative')
  || productionStages.value.find(stage => completionFor(stage)?.percent !== 100)
  || productionStages.value[productionStages.value.length - 1]!)
function completionNote(stage: typeof STAGE_DEFS[number]) {
  const value = completionFor(stage)
  if (!value) return '进度暂时无法读取'
  if (stage.key === 'preview') return `${value.completed.toLocaleString()} / ${value.total.toLocaleString()} 段 · 已解析台词`
  if (value.percent === null) return stage.key === 'audio' ? '确定分集计划后统计' : `当前 ${value.completed} / ${value.total} ${value.unit} · 总量待解析`
  return `${value.completed.toLocaleString()} / ${value.total.toLocaleString()} ${value.unit}`
}
function nextStageReason() {
  const stage = nextStage.value
  if (stageStatus(stage).tone === 'negative') return '此阶段有失败任务，请先检查并处理。'
  const value = completionFor(stage)
  if (value?.percent == null) return completionNote(stage)
  if (value.percent === 100) return '制作已完成，可进入阶段继续调整。'
  return `已完成 ${value.completed} / ${value.total} ${value.unit}，继续完成剩余内容。`
}
const stageDescriptions: Record<string, string> = {
  text: '整理原稿，核对章节与分册',
  script: '解析台词，识别角色与语气',
  voices: '制作候选音色，确定角色声音',
  batch: '按章节生成台词音频',
  preview: '逐句试听，修订台词与声音',
  merge: '将台词音频合并为整章',
  audio: '按时长切分发布音频',
  bgm: '匹配音乐，混音与试听',
}
const recentFailures = computed(() => tasks.value.filter((task) => ['failed', 'timeout'].includes(task.status)).slice(0, 3))

async function load() {
  const generation = ++loadGeneration
  const id = projectId.value
  refreshing.value = true
  error.value = ''
  try {
    if (project.activeProjectId !== id) await project.select(id)
    else if (!project.loaded) await project.refresh()
    if (generation !== loadGeneration || !viewActive) return
    const [taskResult, progressResult] = await Promise.allSettled([
      listDurableTasks(),
      getProjectProgressSummary(id),
    ])
    if (generation !== loadGeneration || id !== projectId.value || !viewActive) return
    tasks.value = taskResult.status === 'fulfilled'
      ? taskResult.value.filter((task) => task.project_id === id)
      : []
    if (progressResult.status === 'fulfilled') summary.value = progressResult.value
    else {
      error.value = '项目进度暂时无法读取，请检查连接后重试。'
    }
  } catch (cause: any) {
    if (generation === loadGeneration && viewActive) error.value = cause?.message || '无法打开此项目。'
  } finally {
    if (generation === loadGeneration && viewActive) {
      refreshing.value = false
      loading.value = false
    }
  }
}

function openStage(path: string) {
  void router.push(path)
}

watch(projectId, () => {
  summary.value = null
  tasks.value = []
  loading.value = true
  if (viewActive) void load()
})
watch(() => taskStore.tasks.filter(task => task.project_id === projectId.value)
  .map(task => `${task.id}:${task.status}:${task.progress}`).join('|'), () => {
  if (!viewActive) return
  clearTimeout(progressTimer)
  progressTimer = setTimeout(() => { void load() }, 500)
})
onActivated(() => {
  viewActive = true
  taskStore.setTaskCenterOpen(true)
  void load()
})
function deactivate() {
  viewActive = false
  loadGeneration += 1
  clearTimeout(progressTimer)
  taskStore.setTaskCenterOpen(false)
}
onDeactivated(deactivate)
onUnmounted(deactivate)
</script>

<template>
  <div class="project-overview viewport-page">
    <header class="page-header">
      <div class="overview-heading">
        <div class="min-w-0">
          <p class="eyebrow">PROJECT · WORKSPACE</p>
          <h1 class="page-title">{{ project.activeProjectName || project.activeProject?.name || '项目' }}</h1>
          <p class="page-description">查看制作状态，进入阶段继续制作。</p>
        </div>
        <RouterLink class="back-link" to="/dashboard"><ArrowLeft class="h-4 w-4" />所有项目</RouterLink>
      </div>
    </header>

    <div v-if="error" class="project-alert" role="alert"><span>{{ error }}</span><Button variant="outline" size="sm" @click="load">重试</Button></div>

    <WorkbenchContextBar class="overview-context" :aria-busy="refreshing">
      <template #icon><FileAudio2 /></template>
      <template #title>制作总览</template>
      <template #description>按实际完成量统计，文件存在不等于制作完成</template>
      <template #metrics>
        <div class="workbench-context-metric"><strong>{{ loading ? '—' : doneStages }} / {{ STAGES.length }}</strong>已完成阶段</div>
        <div class="workbench-context-metric"><strong class="!text-amber-600 dark:!text-amber-400">{{ loading ? '—' : STAGES.length - doneStages }}</strong>待推进</div>
        <div class="workbench-context-metric"><strong>{{ loading ? '—' : unknownStages }}</strong>总量待确定</div>
      </template>
      <template #actions><Button variant="ghost" size="sm" :disabled="refreshing" @click="load"><RefreshCw class="h-4 w-4" :class="{ 'animate-spin': refreshing }" />刷新</Button></template>
    </WorkbenchContextBar>

    <div class="overview-workspace">
      <Card class="stage-panel">
        <div class="panel-heading"><div><h2>制作流程</h2><p>点击阶段进入工作台</p></div><span>{{ STAGES.length }} 个阶段</span></div>
        <div class="stage-columns" aria-hidden="true"><span>阶段 / 功能</span><span>状态</span><span>完成进度 / 数量</span></div>
        <div class="stage-list">
          <div v-if="loading" class="stage-loading" role="status"><LoaderCircle class="h-5 w-5 animate-spin" />正在读取制作进度</div>
          <template v-else>
            <button v-for="(stage, index) in STAGES" :key="stage.key" type="button" class="stage-row" :class="{ 'is-next': stage.key === nextStage.key }" @click="openStage(stage.path)">
              <span class="stage-row__identity"><span class="stage-number">{{ String(index + 1).padStart(2, '0') }}</span><span class="stage-icon"><component :is="stage.icon" class="h-4 w-4" /></span><span class="stage-copy"><strong>{{ stage.label }}<small v-if="stage.key === nextStage.key">建议下一步</small></strong><span>{{ stageDescriptions[stage.key] }}</span></span></span>
              <StatusPill :label="stageStatus(stage).label" :tone="stageStatus(stage).tone" />
              <span class="stage-completion" :style="{ '--progress-color': stageProgressColor(completionFor(stage)?.percent ?? 0) }">
                <span class="stage-completion__heading"><span>{{ stage.key === 'preview' ? '可预览比例' : completionFor(stage)?.percent == null ? '总量待确定' : '完成比例' }}</span><strong>{{ completionFor(stage)?.percent == null ? '—' : `${completionFor(stage)!.percent}%` }}</strong></span>
                <span class="stage-progress" :class="{ 'is-unknown': completionFor(stage)?.percent == null }" role="progressbar" :aria-label="`${stage.label}：${completionNote(stage)}`" :aria-valuenow="completionFor(stage)?.percent ?? undefined" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${completionFor(stage)?.percent ?? 0}%` }" /></span>
                <span class="stage-completion__note">{{ completionNote(stage) }}</span>
              </span>
            </button>
          </template>
        </div>
        <p class="stage-footnote">红色 → 紫色表示完成比例；整章预览按已解析台词的有效音频统计，试听为可选操作。</p>
      </Card>

      <aside class="overview-sidebar" aria-label="项目操作与任务">
        <Card class="next-action">
          <span class="section-eyebrow">建议下一步</span>
          <div v-if="loading" class="stage-loading"><LoaderCircle class="h-4 w-4 animate-spin" />正在读取</div>
          <template v-else>
            <span class="next-action__icon"><component :is="nextStage.icon" class="h-6 w-6" /></span>
            <h2>{{ nextStage.label }}</h2>
            <p>{{ stageDescriptions[nextStage.key] }}</p>
            <p class="next-action__reason">{{ nextStageReason() }}</p>
            <Button class="next-action__button" @click="openStage(nextStage.path)">进入{{ nextStage.label }}<ArrowRight class="h-4 w-4" /></Button>
          </template>
          <div class="import-action"><span>添加或更换原稿</span><Button variant="ghost" size="sm" @click="openStage('/text')"><Upload class="h-4 w-4" />导入原文</Button></div>
        </Card>

        <Card class="attention-card">
          <div class="attention-heading"><h2>需要处理</h2><span v-if="!loading">{{ tasks.filter(task => ['failed', 'timeout'].includes(task.status)).length }}</span></div>
          <div v-if="loading" class="stage-loading" role="status"><LoaderCircle class="h-4 w-4 animate-spin" />正在读取任务</div>
          <div v-else-if="recentFailures.length" class="failure-list">
            <div v-for="task in recentFailures" :key="task.id" class="failure-row"><CircleAlert class="h-4 w-4" /><div><strong>{{ taskTypeLabel(task.task_type) }}</strong><p>{{ task.error_message || '任务失败，请重试或检查输入。' }}</p></div></div>
          </div>
          <div v-else class="attention-empty"><CheckCircle2 class="h-5 w-5" /><div><strong>暂无失败任务</strong><p>可继续进入阶段制作</p></div></div>
          <Button variant="ghost" size="sm" class="task-link" @click="openStage('/tasks')">查看任务中心<ArrowRight class="h-3.5 w-3.5" /></Button>
        </Card>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.project-overview{width:100%;min-width:0}
.overview-heading{display:flex;justify-content:space-between;align-items:center;gap:24px}
.back-link{display:inline-flex;align-items:center;gap:6px;flex:none;color:hsl(var(--muted-foreground));font-size:12px;font-weight:600;padding:8px 0}
.back-link:hover{color:hsl(var(--primary))}
.overview-context{flex:none}
.stage-icon,.next-action__icon{display:grid;place-items:center;flex:none;color:hsl(var(--primary));background:hsl(var(--primary)/.08);border-radius:9px}
.overview-workspace{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:14px;flex:1;min-height:0;min-width:0}
.stage-panel{display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}
.panel-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 18px;flex:none}
.panel-heading h2,.attention-heading h2{font-size:14px;font-weight:700}
.panel-heading>span{font-size:11px;color:hsl(var(--muted-foreground))}
.stage-columns{display:grid;grid-template-columns:minmax(0,1fr) 90px 220px;gap:18px;padding:8px 18px;background:hsl(var(--muted)/.3);border-top:1px solid hsl(var(--border));border-bottom:1px solid hsl(var(--border));font-size:10px;color:hsl(var(--muted-foreground));flex:none}
.stage-list{display:flex;flex-direction:column;flex:1;min-height:0;overflow:auto}
.stage-row{display:grid;grid-template-columns:minmax(0,1fr) 90px 220px;gap:18px;align-items:center;text-align:left;padding:8px 18px;flex:1 0 62px;min-height:62px;border-bottom:1px solid hsl(var(--border)/.7);transition:background .15s}
.stage-row:last-child{border-bottom:0}
.stage-row:hover{background:hsl(var(--primary)/.04)}
.stage-row:focus-visible{outline:2px solid hsl(var(--ring));outline-offset:-2px}
.stage-row.is-next{background:hsl(var(--primary)/.045);box-shadow:inset 3px 0 hsl(var(--primary))}
.stage-row__identity{display:flex;align-items:center;gap:12px;min-width:0}
.stage-number{font-size:11px;font-weight:500;color:hsl(var(--muted-foreground));font-variant-numeric:tabular-nums;flex:none;width:18px}
.stage-icon{width:30px;height:30px}
.stage-copy{min-width:0;display:grid;gap:4px}
.stage-copy>strong{font-size:13px;font-weight:650;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.stage-copy small{font-size:9px;color:hsl(var(--primary));font-weight:500}
.stage-copy>span{font-size:11px;color:hsl(var(--muted-foreground))}
.stage-completion{display:grid;gap:6px;min-width:0;font-variant-numeric:tabular-nums}
.stage-completion__heading{display:flex;align-items:center;justify-content:space-between;gap:8px}
.stage-completion__heading>span{font-size:10px;color:hsl(var(--muted-foreground))}
.stage-completion__heading strong{font-size:15px;line-height:1;font-weight:700}
.stage-progress{display:block;height:5px;border-radius:5px;background:rgb(var(--progress-color)/.16);overflow:hidden}
.stage-progress>span{display:block;height:100%;min-width:2px;border-radius:inherit;background:rgb(var(--progress-color));transition:width .45s,background-color .45s}
.stage-progress.is-unknown{background:hsl(var(--muted))}
.stage-progress.is-unknown>span{display:none}
.stage-completion__note{font-size:10px;line-height:1.4;color:hsl(var(--muted-foreground))}
.next-action>p.next-action__reason{padding:9px 10px;background:hsl(var(--primary)/.05);border-radius:8px;color:hsl(var(--foreground));font-size:11px;margin-top:12px}
@media(prefers-reduced-motion:reduce){.stage-progress>span{transition:none}}
.stage-footnote{font-size:10px;color:hsl(var(--muted-foreground));padding:10px 18px;border-top:1px solid hsl(var(--border));flex:none}
.overview-sidebar{display:flex;flex-direction:column;gap:14px;min-width:0;min-height:0;overflow:auto}
.next-action{padding:16px;flex:none}
.section-eyebrow{display:block;font-size:11px;color:hsl(var(--muted-foreground));font-weight:600}
.next-action__icon{width:38px;height:38px;margin-top:14px}
.next-action h2{font-size:20px;line-height:1.3;font-weight:750;margin-top:14px}
.next-action>p{font-size:12px;line-height:1.7;color:hsl(var(--muted-foreground));margin-top:6px}
.next-action__button{width:100%;margin-top:14px;justify-content:space-between}
.import-action{display:flex;align-items:center;justify-content:space-between;gap:8px;border-top:1px solid hsl(var(--border));padding-top:10px;margin-top:14px;font-size:10px;color:hsl(var(--muted-foreground))}
.attention-card{padding:18px;flex:1;display:flex;flex-direction:column;min-height:180px}
.attention-heading{display:flex;align-items:center;justify-content:space-between;gap:10px}
.attention-heading>span{font-size:11px;color:hsl(var(--muted-foreground));font-variant-numeric:tabular-nums}
.attention-empty{display:flex;align-items:flex-start;gap:10px;margin:22px 0;color:hsl(var(--muted-foreground))}
.attention-empty>svg{flex:none;color:hsl(var(--success))}
.attention-empty strong{font-size:12px;font-weight:600;color:hsl(var(--foreground))}
.attention-empty p{font-size:11px;margin-top:5px;line-height:1.6}
.task-link{margin-top:auto;align-self:flex-start}
.failure-list{display:grid;gap:12px;margin:18px 0}
.failure-row{display:flex;align-items:flex-start;gap:8px}
.failure-row>svg{flex:none;color:hsl(var(--destructive));margin-top:2px}
.failure-row strong{font-size:12px;font-weight:600}
.failure-row p{font-size:11px;line-height:1.6;color:hsl(var(--muted-foreground));margin-top:4px;overflow-wrap:anywhere}
.stage-loading{display:flex;align-items:center;justify-content:center;gap:8px;flex:1;padding:24px;font-size:12px;color:hsl(var(--muted-foreground))}
.project-alert{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 14px;border:1px solid hsl(var(--destructive)/.3);border-radius:12px;color:hsl(var(--destructive));font-size:12px;flex:none}
@media(max-height:780px) and (min-width:761px){.stage-row{flex-basis:48px;min-height:48px;padding-top:5px;padding-bottom:5px}.stage-completion{gap:3px}.stage-progress{height:4px}.stage-completion__note{font-size:9px}.next-action__icon{display:none}.next-action h2{margin-top:10px}.next-action__button{margin-top:10px}.next-action>p.next-action__reason{margin-top:8px}.import-action{margin-top:10px;padding-top:8px}.attention-card{min-height:150px}.stage-copy{gap:3px}}
@media(max-width:1200px){.overview-workspace{grid-template-columns:minmax(0,1fr) 240px}.stage-columns,.stage-row{grid-template-columns:minmax(0,1fr) 76px minmax(145px,30%);gap:12px;padding-left:14px;padding-right:14px}.stage-row__identity{gap:8px}.stage-icon{display:none}.next-action{padding:16px}.stage-copy small{display:none}}
@media(max-width:760px){.overview-workspace{display:flex;flex-direction:column;overflow:auto}.stage-panel{flex:none;min-height:560px}.overview-sidebar{overflow:visible;flex:none;display:grid;grid-template-columns:1fr 1fr}.overview-heading{gap:10px}.back-link{font-size:10px}.stage-copy small{display:none}.attention-card{min-height:0}}
@media(max-width:520px){.overview-sidebar{grid-template-columns:1fr}.stage-copy>span{display:none}.stage-row,.stage-columns{grid-template-columns:minmax(0,1fr) 68px 125px;gap:8px}.stage-number{display:none}.stage-copy>strong{font-size:12px}.stage-completion__note{font-size:9px}}
</style>
