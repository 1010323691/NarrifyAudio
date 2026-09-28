<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, ArrowRight, AudioLines, CircleAlert, FileAudio2, FileText, LoaderCircle, RefreshCw, Upload } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { listDir } from '@/api/files'
import { listDurableTasks, type DurableTask } from '@/api/durableTasks'
import { useProjectStore } from '@/stores/project'
import { useSettingsStore } from '@/stores/settings'
import { taskTypeLabel } from '@/utils/taskLabels'
import { modulePrefixes } from '@/utils/taskTypes'

const route = useRoute()
const router = useRouter()
const project = useProjectStore()
const settings = useSettingsStore()
const projectId = computed(() => String(route.params.projectId || ''))
const loading = ref(true)
const error = ref('')
const artifactCounts = ref<Record<string, number>>({})
const tasks = ref<DurableTask[]>([])
const refreshing = ref(false)
const SPLIT_VOLUME_NAME = /^第\s+\d+\s+章(?:\s|\.|$)/

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
  if ((artifactCounts.value[stage.dir] || 0) > 0) return { label: '已有产物', tone: 'positive' as const }
  return { label: '待开始', tone: 'neutral' as const }
}

const doneStages = computed(() => STAGES.value.filter((stage) => (artifactCounts.value[stage.dir] || 0) > 0).length)
const nextStage = computed(() => STAGES.value.find((stage) => ['待开始', '需处理'].includes(stageStatus(stage).label)) || STAGES.value[STAGES.value.length - 1])
const recentFailures = computed(() => tasks.value.filter((task) => ['failed', 'timeout'].includes(task.status)).slice(0, 3))

async function load() {
  // 首帧骨架由 loading（初始 true）驱动：任务列表与各阶段文件数到达前不渲染
  // 「待开始 / 0 个文件」的假状态；刷新按钮在 refreshing 期间禁用，数据落定后
  // finally 统一收尾，后续「刷新进度」保留现值并用底部 spinner 提示。
  refreshing.value = true
  error.value = ''
  try {
    if (project.activeProjectId !== projectId.value) await project.select(projectId.value)
    else if (!project.loaded) await project.refresh()
    const [taskResult, ...fileResults] = await Promise.allSettled([
      listDurableTasks(),
      ...STAGES.value.map((stage) => listDir(stage.dir, true)),
    ])
    tasks.value = taskResult.status === 'fulfilled'
      ? taskResult.value.filter((task) => task.project_id === projectId.value)
      : []
    const counts: Record<string, number> = {}
    fileResults.forEach((result, index) => {
      const stage = STAGES.value[index]
      const files = result.status === 'fulfilled' ? result.value.items.filter((item) => !item.is_dir) : []
      counts[stage.dir] = stage.key === 'text'
        ? files.filter((item) => SPLIT_VOLUME_NAME.test(item.name.split('/').pop() || '')).length
        : files.length
    })
    artifactCounts.value = counts
    if (taskResult.status === 'rejected' && fileResults.every((result) => result.status === 'rejected')) {
      error.value = '项目进度暂时无法读取，请检查连接后重试。'
    }
  } catch (cause: any) {
    error.value = cause?.message || '无法打开此项目。'
  } finally {
    refreshing.value = false
    loading.value = false
  }
}

function openStage(path: string) {
  void router.push(path)
}

onMounted(load)
</script>

<template>
  <div class="project-overview">
    <header class="project-overview__head">
      <div>
        <RouterLink class="back-link" to="/dashboard"><ArrowLeft class="h-3.5 w-3.5" />所有项目</RouterLink>
        <p class="eyebrow">项目工作台</p>
        <h1>{{ project.activeProjectName || project.activeProject?.name || '项目' }}</h1>
        <p class="muted">查看进度，继续下一步制作。</p>
      </div>
      <div class="project-overview__actions">
        <Button variant="outline" :disabled="refreshing" @click="load"><RefreshCw class="h-4 w-4" />刷新进度</Button>
        <Button @click="openStage('/text')"><Upload class="h-4 w-4" />导入原文</Button>
      </div>
    </header>

    <div v-if="error" class="project-alert" role="alert"><span>{{ error }}</span><Button variant="outline" size="sm" @click="load">重试</Button></div>

    <Card class="project-progress">
      <div class="project-progress__top">
        <div><span class="muted">制作进度</span><strong>{{ doneStages }} / {{ STAGES.length }} 阶段已有产物</strong></div>
        <StatusPill v-if="loading" label="正在加载…" tone="neutral" />
        <StatusPill v-else-if="recentFailures.length" label="有任务需处理" tone="negative" />
        <StatusPill v-else label="项目可继续制作" tone="positive" />
      </div>
      <div class="project-progress__bar"><span :style="{ width: `${(doneStages / STAGES.length) * 100}%` }" /></div>
      <div class="project-progress__next">
        <div><span class="muted">建议下一步</span><strong>{{ nextStage.label }}</strong></div>
        <Button size="sm" @click="openStage(nextStage.path)">继续制作<ArrowRight class="h-4 w-4" /></Button>
      </div>
    </Card>

    <section class="stage-section">
      <div class="section-title"><div><h2>制作流程</h2><p>阶段完成后可以直接进入任意步骤继续调整。</p></div></div>
      <div v-if="loading" class="stage-grid" aria-label="正在加载项目进度">
        <Card v-for="n in 4" :key="n" class="stage-skeleton"><div class="skeleton-line w-1/3" /><div class="skeleton-line w-2/3" /></Card>
      </div>
      <div v-else class="stage-grid">
        <button v-for="(stage, index) in STAGES" :key="stage.key" type="button" class="stage-card" @click="openStage(stage.path)">
          <div class="stage-card__top"><span class="stage-number">{{ String(index + 1).padStart(2, '0') }}</span><component :is="stage.icon" class="h-4 w-4" /></div>
          <strong>{{ stage.label }}</strong>
          <div class="stage-card__status"><StatusPill :label="stageStatus(stage).label" :tone="stageStatus(stage).tone" /><span>{{ artifactCounts[stage.dir] || 0 }} {{ stage.key === 'text' ? '个分册' : '个文件' }}</span></div>
          <ArrowRight class="stage-card__arrow h-4 w-4" />
        </button>
      </div>
    </section>

    <section class="project-bottom">
      <Card class="next-action">
        <div class="next-action__icon"><ArrowRight class="h-5 w-5" /></div>
        <div><p class="muted">从这里继续</p><h2>{{ nextStage.label }}</h2><p class="muted">阶段内部设置和工具保持原样。</p></div>
        <Button @click="openStage(nextStage.path)">打开阶段<ArrowRight class="h-4 w-4" /></Button>
      </Card>
      <Card class="attention-card">
        <div class="section-title section-title--compact"><div><h2>最近需要处理</h2><p>仅包含此项目的失败任务。</p></div><CircleAlert class="h-4 w-4" /></div>
        <div v-if="loading" class="stage-skeleton" aria-label="正在读取任务状态"><div class="skeleton-line w-2/3" /></div>
        <div v-else-if="recentFailures.length" class="failure-list">
          <div v-for="task in recentFailures" :key="task.id" class="failure-row"><div><strong>{{ taskTypeLabel(task.task_type) }}</strong><small>{{ task.error_message || '任务失败，请重试或检查输入。' }}</small></div><StatusPill label="失败" tone="negative" /></div>
        </div>
        <p v-else class="empty-inline">目前没有失败任务。</p>
      </Card>
    </section>
    <div v-if="refreshing && !loading" class="refreshing-note"><LoaderCircle class="h-3.5 w-3.5 animate-spin" />正在更新项目状态</div>
  </div>
</template>

<style scoped>
.project-overview{display:grid;gap:22px;max-width:1320px;margin:0 auto;padding-bottom:30px}.project-overview__head{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap}.back-link{display:inline-flex;align-items:center;gap:4px;color:hsl(var(--muted-foreground));font-size:11px;font-weight:600}.back-link:hover{color:hsl(var(--primary))}.eyebrow{margin-top:13px;font-size:10px;font-weight:800;letter-spacing:.14em;color:hsl(var(--primary))}.project-overview__head h1{margin-top:3px;font-size:26px;font-weight:750}.muted,.section-title p,.empty-inline{color:hsl(var(--muted-foreground));font-size:12px}.project-overview__head .muted{margin-top:4px}.project-overview__actions{display:flex;gap:8px;flex-wrap:wrap}.project-progress{padding:16px 18px}.project-progress__top,.project-progress__next{display:flex;align-items:center;justify-content:space-between;gap:12px}.project-progress__top>div,.project-progress__next>div{display:grid;gap:4px}.project-progress__top strong,.project-progress__next strong{font-size:14px}.project-progress__bar{height:6px;margin:14px 0;border-radius:99px;background:hsl(var(--muted));overflow:hidden}.project-progress__bar span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,hsl(var(--primary)),hsl(199 89% 48%));transition:width .2s}.project-progress__next{border-top:1px solid hsl(var(--border));padding-top:13px}.stage-section{display:grid;gap:12px}.section-title{display:flex;align-items:center;justify-content:space-between;gap:10px}.section-title h2{font-size:16px;font-weight:750}.section-title p{margin-top:3px}.stage-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.stage-card{position:relative;display:grid;gap:12px;min-height:128px;border:1px solid var(--glass-border);border-radius:11px;background:var(--glass-tint);box-shadow:var(--glass-highlight),var(--glass-shadow);padding:14px;text-align:left;transition:border-color .14s,transform .14s,box-shadow .14s}.stage-card:hover{transform:translateY(-1px);border-color:hsl(var(--primary)/.45);box-shadow:var(--glass-highlight),0 6px 16px hsl(var(--foreground)/.07)}.stage-card__top{display:flex;justify-content:space-between;align-items:center;color:hsl(var(--primary))}.stage-number{font-size:10px;font-weight:800;letter-spacing:.1em;color:hsl(var(--muted-foreground))}.stage-card>strong{font-size:13px}.stage-card__status{display:flex;align-items:center;justify-content:space-between;gap:8px}.stage-card__status>span{color:hsl(var(--muted-foreground));font-size:10px}.stage-card__arrow{position:absolute;right:13px;top:44px;color:hsl(var(--muted-foreground));opacity:0;transition:opacity .15s}.stage-card:hover .stage-card__arrow{opacity:1}.project-bottom{display:grid;grid-template-columns:1fr 1fr;gap:12px}.next-action{display:flex;align-items:center;gap:13px;padding:16px}.next-action__icon{display:grid;place-items:center;width:40px;height:40px;flex:none;border-radius:11px;background:hsl(var(--primary)/.09);color:hsl(var(--primary))}.next-action>div:nth-child(2){min-width:0;flex:1}.next-action h2{margin:2px 0;font-size:14px;font-weight:700}.next-action>div:nth-child(2) p:last-child{font-size:10px}.attention-card{padding:15px}.section-title--compact{margin-bottom:12px}.section-title--compact svg{color:hsl(var(--muted-foreground))}.failure-list{display:grid;gap:7px}.failure-row{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;border-top:1px solid hsl(var(--border));padding-top:9px}.failure-row strong,.failure-row small{display:block}.failure-row strong{font-size:11px}.failure-row small{max-width:360px;margin-top:3px;color:hsl(var(--muted-foreground));font-size:10px;overflow-wrap:anywhere}.stage-skeleton{display:grid;gap:14px;padding:15px}.skeleton-line{height:11px;border-radius:6px;background:hsl(var(--muted));animation:pulse 1.4s ease-in-out infinite}.stage-skeleton .w-1\/3{width:33%}.stage-skeleton .w-2\/3{width:66%}.refreshing-note{display:flex;align-items:center;gap:5px;color:hsl(var(--muted-foreground));font-size:10px}.project-alert{display:flex;justify-content:space-between;align-items:center;gap:12px;border:1px solid hsl(var(--destructive)/.25);border-radius:10px;background:hsl(var(--destructive)/.06);padding:10px 12px;color:hsl(var(--destructive));font-size:12px}@keyframes pulse{50%{opacity:.4}}@media(max-width:900px){.stage-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.project-bottom{grid-template-columns:1fr}}@media(max-width:600px){.project-overview__head h1{font-size:22px}.project-overview__actions{width:100%}.project-overview__actions>*{flex:1}.stage-grid{grid-template-columns:1fr}.project-progress__top{align-items:flex-start}.next-action{flex-wrap:wrap}.next-action>div:nth-child(2){min-width:calc(100% - 55px)}.next-action>button{width:100%}}
</style>
