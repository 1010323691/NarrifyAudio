<script setup lang="ts">
import { computed } from 'vue'
import {
  Activity,
  CheckCircle2,
  CircleDashed,
  Clock3,
  Cpu,
  Inbox,
  Layers3,
  LoaderCircle,
  OctagonAlert,
  Play,
  Radio,
  Send,
  Sparkles,
  TimerReset,
  Zap,
} from 'lucide-vue-next'
import type { AdminTask, QueueStatus, TaskMetrics, WorkerStatus } from '@/api/admin'

const props = defineProps<{
  tasks: AdminTask[]
  workers: WorkerStatus[]
  queue: QueueStatus | null
  metrics: TaskMetrics | null
  loading?: boolean
}>()

const stageDefinitions = [
  { key: 'production', label: '生产', caption: '刚刚创建', icon: Send, tone: 'violet' },
  { key: 'queued', label: '排队', caption: '等待公平调度', icon: Inbox, tone: 'amber' },
  { key: 'consuming', label: '消费', caption: 'Worker 正在处理', icon: Cpu, tone: 'blue' },
  { key: 'completed', label: '完成', caption: '已写入结果', icon: CheckCircle2, tone: 'emerald' },
] as const

const stageCounts = computed(() => props.metrics?.stage_counts ?? {
  production: props.tasks.filter(task => task.status === 'pending').length,
  queued: props.tasks.filter(task => ['queued', 'retrying', 'paused'].includes(task.status)).length,
  consuming: props.tasks.filter(task => ['running', 'cancelling'].includes(task.status)).length,
  completed: props.tasks.filter(task => task.status === 'succeeded').length,
  attention: props.tasks.filter(task => ['failed', 'timeout', 'cancelled'].includes(task.status)).length,
})

const taskTypes = computed(() => {
  const rows = props.metrics?.by_type ?? []
  if (rows.length) return rows.slice().sort((a, b) => b.total - a.total)
  const grouped = new Map<string, number>()
  for (const task of props.tasks) grouped.set(task.task_type, (grouped.get(task.task_type) ?? 0) + 1)
  return [...grouped.entries()].map(([task_type, total]) => ({ task_type, total, statuses: {} })).sort((a, b) => b.total - a.total)
})

const maxTypeTotal = computed(() => Math.max(1, ...taskTypes.value.map(item => item.total)))
const activeWorkers = computed(() => props.workers.filter(worker => ['busy', 'running', 'working'].includes(worker.status)).length)
const liveTasks = computed(() => props.tasks.slice().sort((a, b) => Date.parse(b.updated_at || b.created_at) - Date.parse(a.updated_at || a.created_at)).slice(0, 7))

function typeLabel(value: string): string {
  const labels: Record<string, string> = {
    'tts.batch': 'TTS 批量合成',
    'tts.merge': '音频合并',
    'tts.stress': 'TTS 压测',
    'voices.foundation': '声音基础生成',
    'voices.clone': '声音克隆',
    'bgm.analysis': 'BGM 分析',
    'bgm.segment': 'BGM 段落分析',
    'bgm.match': 'BGM 匹配',
    'bgm.mix': 'BGM 混音',
    'music.suggest_tags': '音乐标签建议',
    'audio.silences': '静音检测',
    'audio.cut': '音频切分',
    'audio.zip': '音频打包',
    'audio.export': '音频导出',
    'text.format': '文本排版',
    'book.analyze': '书籍分析',
    'book.split': '书籍拆分',
    'script.parse': '脚本解析',
  }
  return labels[value] ?? value
}

function taskStage(status: string): string {
  if (status === 'pending') return '生产'
  if (['queued', 'retrying', 'paused'].includes(status)) return '排队'
  if (['running', 'cancelling'].includes(status)) return '消费'
  if (status === 'succeeded') return '完成'
  return '异常'
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: '生产中', queued: '排队中', retrying: '重试等待', paused: '已暂停',
    running: '消费中', cancelling: '取消中', succeeded: '已完成', failed: '失败',
    timeout: '超时', cancelled: '已取消',
  }
  return labels[status] ?? status
}

function statusTone(status: string): string {
  if (status === 'succeeded') return 'positive'
  if (['failed', 'timeout'].includes(status)) return 'negative'
  if (['running', 'cancelling'].includes(status)) return 'active'
  if (['pending', 'queued', 'retrying', 'paused'].includes(status)) return 'waiting'
  return 'muted'
}

function timeLabel(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
</script>

<template>
  <section class="ops-dashboard" aria-labelledby="ops-dashboard-title">
    <div class="ops-dashboard__heading">
      <div>
        <div class="ops-dashboard__eyebrow"><Activity class="h-3.5 w-3.5" aria-hidden="true" />LIVE OPERATIONS</div>
        <h2 id="ops-dashboard-title" class="ops-dashboard__title">任务生产线</h2>
        <p class="ops-dashboard__description">从用户提交到 Worker 消费的全链路状态，按用户公平调度。</p>
      </div>
      <div class="ops-dashboard__telemetry" aria-live="polite">
        <span class="ops-pulse" aria-hidden="true" />
        <span>{{ activeWorkers }} 个 Worker 活跃</span>
        <span class="ops-dashboard__telemetry-divider" aria-hidden="true" />
        <span>{{ metrics?.total ?? tasks.length }} 个任务总计</span>
      </div>
    </div>

    <div class="ops-kpi-grid" aria-label="任务状态摘要">
      <div class="ops-kpi ops-kpi--active">
        <div class="ops-kpi__icon"><Zap class="h-4 w-4" aria-hidden="true" /></div>
        <div><span class="ops-kpi__label">消费中</span><strong>{{ stageCounts.consuming }}</strong><span class="ops-kpi__hint">实时处理</span></div>
      </div>
      <div class="ops-kpi">
        <div class="ops-kpi__icon"><Clock3 class="h-4 w-4" aria-hidden="true" /></div>
        <div><span class="ops-kpi__label">待调度</span><strong>{{ stageCounts.production + stageCounts.queued }}</strong><span class="ops-kpi__hint">生产 + 排队</span></div>
      </div>
      <div class="ops-kpi">
        <div class="ops-kpi__icon"><CheckCircle2 class="h-4 w-4" aria-hidden="true" /></div>
        <div><span class="ops-kpi__label">已完成</span><strong>{{ stageCounts.completed }}</strong><span class="ops-kpi__hint">持久化结果</span></div>
      </div>
      <div class="ops-kpi ops-kpi--attention">
        <div class="ops-kpi__icon"><OctagonAlert class="h-4 w-4" aria-hidden="true" /></div>
        <div><span class="ops-kpi__label">需关注</span><strong>{{ stageCounts.attention }}</strong><span class="ops-kpi__hint">失败 / 超时 / 取消</span></div>
      </div>
    </div>

    <div class="ops-main-grid">
      <div class="ops-panel ops-panel--pipeline">
        <div class="ops-panel__header">
          <div><h3>调度流转</h3><p>数据库状态是权威，Redis 负责唤醒。</p></div>
          <div class="ops-stream-badge"><Radio class="h-3.5 w-3.5" aria-hidden="true" />{{ queue?.available ? 'STREAM ONLINE' : 'STREAM OFFLINE' }}</div>
        </div>
        <div class="ops-pipeline" role="img" aria-label="任务从生产经过排队和消费，最终完成的流程动画">
          <div class="ops-pipeline__rail" aria-hidden="true"><span class="ops-pipeline__line" /><i v-for="n in 4" :key="n" class="ops-pipeline__runner" :style="{ '--runner-delay': `${(n - 1) * 1.05}s` }" /></div>
          <ol class="ops-pipeline__steps">
            <li v-for="(stage, index) in stageDefinitions" :key="stage.key" class="ops-stage" :class="`ops-stage--${stage.tone}`">
              <div class="ops-stage__node"><component :is="stage.icon" class="h-5 w-5" aria-hidden="true" /></div>
              <div class="ops-stage__copy"><span>{{ stage.label }}</span><strong>{{ stageCounts[stage.key] }}</strong><small>{{ stage.caption }}</small></div>
              <span v-if="index < stageDefinitions.length - 1" class="ops-stage__connector" aria-hidden="true"><Play class="h-3 w-3" fill="currentColor" /></span>
            </li>
          </ol>
        </div>
        <div class="ops-pipeline__footer"><span><Layers3 class="h-3.5 w-3.5" aria-hidden="true" />{{ queue?.length ?? 0 }} 条 Stream 消息</span><span><CircleDashed class="h-3.5 w-3.5" aria-hidden="true" />{{ queue?.pending ?? 0 }} 条待确认</span><span class="ops-pipeline__fair"><Sparkles class="h-3.5 w-3.5" aria-hidden="true" />公平轮转已启用</span></div>
      </div>

      <div class="ops-panel ops-panel--workers">
        <div class="ops-panel__header"><div><h3>消费者池</h3><p>Worker 心跳与当前消费任务</p></div><Cpu class="h-5 w-5 text-primary" aria-hidden="true" /></div>
        <div v-if="workers.length" class="ops-workers-list">
          <div v-for="worker in workers.slice(0, 4)" :key="worker.worker_id" class="ops-worker-row">
            <div class="ops-worker-row__avatar"><LoaderCircle v-if="['busy', 'running', 'working'].includes(worker.status)" class="h-4 w-4 ops-spin" aria-hidden="true" /><Cpu v-else class="h-4 w-4" aria-hidden="true" /></div>
            <div class="ops-worker-row__copy"><strong>{{ worker.worker_id }}</strong><span>{{ worker.current_task_id ? `消费 ${worker.current_task_id.slice(0, 8)}…` : '空闲，等待任务' }}</span></div>
            <span class="ops-worker-row__status" :class="worker.status === 'offline' ? 'is-offline' : worker.current_task_id ? 'is-busy' : 'is-idle'"><i aria-hidden="true" />{{ worker.status === 'offline' ? '离线' : worker.current_task_id ? '消费中' : '空闲' }}</span>
          </div>
        </div>
        <div v-else class="ops-empty"><TimerReset class="h-5 w-5" aria-hidden="true" />暂无 Worker 心跳</div>
      </div>
    </div>

    <div class="ops-lower-grid">
      <div class="ops-panel">
        <div class="ops-panel__header"><div><h3>任务类型分布</h3><p>覆盖全部持久化任务，不受最近 500 条限制。</p></div><Layers3 class="h-5 w-5 text-primary" aria-hidden="true" /></div>
        <div v-if="taskTypes.length" class="ops-type-list">
          <div v-for="item in taskTypes" :key="item.task_type" class="ops-type-row">
            <div class="ops-type-row__label"><span>{{ typeLabel(item.task_type) }}</span><code>{{ item.task_type }}</code></div>
            <div class="ops-type-row__bar"><span :style="{ width: `${Math.max(7, item.total / maxTypeTotal * 100)}%` }" /></div>
            <strong>{{ item.total }}</strong>
          </div>
        </div>
        <div v-else class="ops-empty"><Layers3 class="h-5 w-5" aria-hidden="true" />暂无任务类型数据</div>
      </div>

      <div class="ops-panel">
        <div class="ops-panel__header"><div><h3>实时活动</h3><p>最近更新的任务状态</p></div><Activity class="h-5 w-5 text-primary" aria-hidden="true" /></div>
        <TransitionGroup v-if="liveTasks.length" name="ops-list" tag="div" class="ops-live-list" aria-live="polite">
          <div v-for="task in liveTasks" :key="task.id" class="ops-live-row">
            <span class="ops-live-row__marker" :class="`is-${statusTone(task.status)}`" aria-hidden="true"><i /></span>
            <div class="ops-live-row__copy"><strong>{{ typeLabel(task.task_type) }}</strong><span>{{ task.owner_username }} · {{ timeLabel(task.updated_at || task.created_at) }}</span></div>
            <div class="ops-live-row__meta"><span>{{ taskStage(task.status) }}</span><strong>{{ task.progress }}%</strong></div>
          </div>
        </TransitionGroup>
        <div v-else class="ops-empty"><Activity class="h-5 w-5" aria-hidden="true" />暂无实时活动</div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ops-dashboard { --ops-ink: hsl(var(--foreground)); --ops-muted: hsl(var(--muted-foreground)); --ops-line: hsl(var(--border)); margin-bottom: 28px; }
.ops-dashboard__heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 16px; }
.ops-dashboard__eyebrow { display: inline-flex; align-items: center; gap: 6px; color: hsl(var(--primary)); font-size: 10px; font-weight: 800; letter-spacing: .14em; }
.ops-dashboard__title { margin-top: 6px; color: var(--ops-ink); font-size: 22px; font-weight: 800; letter-spacing: -.03em; }
.ops-dashboard__description { margin-top: 4px; color: var(--ops-muted); font-size: 12px; }
.ops-dashboard__telemetry { display: inline-flex; align-items: center; gap: 8px; color: var(--ops-muted); font-size: 11px; font-weight: 700; white-space: nowrap; }
.ops-dashboard__telemetry-divider { width: 1px; height: 13px; background: var(--ops-line); }
.ops-pulse { width: 7px; height: 7px; border-radius: 999px; background: hsl(158 64% 42%); box-shadow: 0 0 0 4px hsl(158 64% 42% / .12); animation: ops-pulse 1.8s ease-in-out infinite; }
.ops-kpi-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
.ops-kpi { display: flex; align-items: center; gap: 11px; min-height: 74px; border: 1px solid var(--ops-line); border-radius: 14px; background: hsl(var(--card)); padding: 12px 14px; }
.ops-kpi--active { border-color: hsl(var(--primary) / .35); background: hsl(var(--accent)); }
.ops-kpi--attention { border-color: hsl(0 72% 51% / .22); }
.ops-kpi__icon { display: grid; width: 32px; height: 32px; flex: 0 0 32px; place-items: center; border-radius: 10px; background: hsl(var(--muted)); color: hsl(var(--primary)); }
.ops-kpi--active .ops-kpi__icon { background: hsl(var(--primary) / .14); }
.ops-kpi--attention .ops-kpi__icon { background: hsl(0 72% 51% / .1); color: hsl(var(--destructive)); }
.ops-kpi__label, .ops-kpi__hint { display: block; color: var(--ops-muted); font-size: 11px; }
.ops-kpi strong { display: inline-block; margin: 1px 6px 0 0; color: var(--ops-ink); font-size: 22px; line-height: 1; }
.ops-kpi__hint { display: inline; font-size: 10px; }
.ops-main-grid, .ops-lower-grid { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(300px, .95fr); gap: 12px; margin-top: 12px; }
.ops-lower-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.ops-panel { min-width: 0; border: 1px solid var(--ops-line); border-radius: 16px; background: hsl(var(--card)); padding: 16px; }
.ops-panel--pipeline { overflow: hidden; }
.ops-panel__header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.ops-panel__header h3 { color: var(--ops-ink); font-size: 14px; font-weight: 800; }
.ops-panel__header p { margin-top: 2px; color: var(--ops-muted); font-size: 11px; }
.ops-stream-badge { display: inline-flex; align-items: center; gap: 5px; border: 1px solid hsl(var(--primary) / .2); border-radius: 999px; background: hsl(var(--accent)); padding: 5px 8px; color: hsl(var(--accent-foreground)); font-size: 9px; font-weight: 800; letter-spacing: .08em; white-space: nowrap; }
.ops-pipeline { position: relative; margin-top: 26px; padding: 0 4px; }
.ops-pipeline__rail { position: absolute; top: 25px; right: 9%; left: 9%; height: 2px; background: hsl(var(--primary) / .13); }
.ops-pipeline__line { position: absolute; inset: 0; background: hsl(var(--primary) / .45); transform-origin: left; animation: ops-line 4.2s ease-in-out infinite; }
.ops-pipeline__runner { position: absolute; top: -3px; left: 0; width: 8px; height: 8px; border-radius: 50%; background: hsl(var(--primary)); box-shadow: 0 0 0 5px hsl(var(--primary) / .12); animation: ops-runner 4.2s linear infinite; animation-delay: var(--runner-delay); }
.ops-pipeline__steps { position: relative; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 0; padding: 0; list-style: none; }
.ops-stage { position: relative; display: flex; min-width: 0; flex-direction: column; align-items: center; text-align: center; }
.ops-stage__node { display: grid; width: 50px; height: 50px; place-items: center; border: 5px solid hsl(var(--card)); border-radius: 16px; background: hsl(var(--muted)); color: var(--ops-muted); box-shadow: 0 0 0 1px var(--ops-line); }
.ops-stage--violet .ops-stage__node { background: hsl(258 80% 96%); color: hsl(258 70% 54%); }
.ops-stage--amber .ops-stage__node { background: hsl(42 95% 93%); color: hsl(32 86% 42%); }
.ops-stage--blue .ops-stage__node { background: hsl(214 95% 94%); color: hsl(214 80% 48%); }
.ops-stage--emerald .ops-stage__node { background: hsl(158 64% 92%); color: hsl(158 64% 34%); }
.ops-stage__copy { min-width: 0; margin-top: 10px; }
.ops-stage__copy span, .ops-stage__copy small { display: block; color: var(--ops-muted); font-size: 11px; }
.ops-stage__copy strong { display: inline-block; margin-right: 4px; color: var(--ops-ink); font-size: 20px; line-height: 1.1; }
.ops-stage__copy small { display: inline; font-size: 10px; }
.ops-stage__connector { position: absolute; top: 19px; right: -8px; display: grid; width: 20px; height: 20px; place-items: center; color: hsl(var(--primary) / .6); }
.ops-pipeline__footer { display: flex; flex-wrap: wrap; gap: 12px 18px; margin-top: 22px; border-top: 1px solid hsl(var(--border) / .75); padding-top: 11px; color: var(--ops-muted); font-size: 10px; }
.ops-pipeline__footer span { display: inline-flex; align-items: center; gap: 5px; }
.ops-pipeline__footer .ops-pipeline__fair { color: hsl(158 64% 35%); font-weight: 700; }
.ops-workers-list, .ops-live-list, .ops-type-list { margin-top: 14px; }
.ops-type-list { max-height: 270px; overflow-y: auto; padding-right: 4px; }
.ops-worker-row, .ops-live-row { display: flex; min-width: 0; align-items: center; gap: 9px; border-top: 1px solid hsl(var(--border) / .7); padding: 10px 0; }
.ops-worker-row:first-child, .ops-live-row:first-child { border-top: 0; padding-top: 2px; }
.ops-worker-row__avatar { display: grid; width: 30px; height: 30px; flex: 0 0 30px; place-items: center; border-radius: 9px; background: hsl(var(--accent)); color: hsl(var(--primary)); }
.ops-worker-row__copy, .ops-live-row__copy { min-width: 0; flex: 1; }
.ops-worker-row__copy strong, .ops-live-row__copy strong { display: block; overflow: hidden; color: var(--ops-ink); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.ops-worker-row__copy span, .ops-live-row__copy span { display: block; overflow: hidden; color: var(--ops-muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.ops-worker-row__status { display: inline-flex; align-items: center; gap: 5px; color: var(--ops-muted); font-size: 10px; font-weight: 700; white-space: nowrap; }
.ops-worker-row__status i { width: 6px; height: 6px; border-radius: 999px; background: currentColor; }
.ops-worker-row__status.is-idle { color: hsl(158 64% 35%); }.ops-worker-row__status.is-busy { color: hsl(var(--primary)); }.ops-worker-row__status.is-offline { color: hsl(var(--destructive)); }
.ops-type-row { display: grid; grid-template-columns: minmax(125px, 1.15fr) minmax(100px, 2fr) 30px; align-items: center; gap: 10px; border-top: 1px solid hsl(var(--border) / .7); padding: 9px 0; }
.ops-type-row:first-child { border-top: 0; padding-top: 2px; }
.ops-type-row__label { min-width: 0; }.ops-type-row__label span, .ops-type-row__label code { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.ops-type-row__label span { color: var(--ops-ink); font-size: 11px; font-weight: 700; }.ops-type-row__label code { color: var(--ops-muted); font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 9px; }
.ops-type-row__bar { overflow: hidden; height: 7px; border-radius: 999px; background: hsl(var(--muted)); }.ops-type-row__bar span { display: block; height: 100%; border-radius: inherit; background: hsl(var(--primary)); transform-origin: left; animation: ops-bar-in 500ms ease both; }
.ops-type-row > strong { color: var(--ops-ink); font-size: 12px; text-align: right; }
.ops-live-row__marker { display: grid; width: 24px; height: 24px; flex: 0 0 24px; place-items: center; border-radius: 8px; background: hsl(var(--muted)); }.ops-live-row__marker i { width: 7px; height: 7px; border-radius: 50%; background: hsl(var(--muted-foreground)); }.ops-live-row__marker.is-active { background: hsl(var(--primary) / .12); }.ops-live-row__marker.is-active i { background: hsl(var(--primary)); animation: ops-pulse 1.4s ease-in-out infinite; }.ops-live-row__marker.is-positive { background: hsl(158 64% 92%); }.ops-live-row__marker.is-positive i { background: hsl(158 64% 35%); }.ops-live-row__marker.is-negative { background: hsl(0 72% 94%); }.ops-live-row__marker.is-negative i { background: hsl(var(--destructive)); }.ops-live-row__marker.is-waiting { background: hsl(42 95% 93%); }.ops-live-row__marker.is-waiting i { background: hsl(32 86% 42%); }
.ops-live-row__meta { display: flex; min-width: 54px; flex-direction: column; align-items: flex-end; color: var(--ops-muted); font-size: 10px; }.ops-live-row__meta strong { color: var(--ops-ink); font-size: 11px; }
.ops-empty { display: flex; min-height: 130px; align-items: center; justify-content: center; gap: 8px; color: var(--ops-muted); font-size: 12px; }
.ops-spin { animation: ops-spin 1.2s linear infinite; }
.ops-list-enter-active, .ops-list-leave-active { transition: opacity 180ms ease, transform 180ms ease; }.ops-list-enter-from, .ops-list-leave-to { opacity: 0; transform: translateY(5px); }
@keyframes ops-pulse { 0%, 100% { opacity: .7; transform: scale(.86); } 50% { opacity: 1; transform: scale(1.08); } }
@keyframes ops-line { 0%, 100% { transform: scaleX(.35); opacity: .45; } 45%, 75% { transform: scaleX(1); opacity: 1; } }
@keyframes ops-runner { 0% { left: 0; opacity: 0; } 10% { opacity: 1; } 85% { opacity: 1; } 100% { left: 100%; opacity: 0; } }
@keyframes ops-spin { to { transform: rotate(360deg); } }
@keyframes ops-bar-in { from { transform: scaleX(0); } to { transform: scaleX(1); } }
@media (max-width: 980px) { .ops-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.ops-main-grid, .ops-lower-grid { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .ops-dashboard__heading { display: block; }.ops-dashboard__telemetry { margin-top: 12px; }.ops-kpi-grid { grid-template-columns: 1fr 1fr; gap: 7px; }.ops-kpi { min-height: 66px; padding: 10px; }.ops-kpi__icon { display: none; }.ops-kpi strong { font-size: 19px; }.ops-kpi__hint { display: block; }.ops-pipeline { margin-top: 20px; }.ops-pipeline__rail { top: 21px; }.ops-stage__node { width: 42px; height: 42px; border-radius: 13px; }.ops-stage__connector { top: 13px; right: -9px; }.ops-stage__copy small { display: block; margin-top: 2px; }.ops-pipeline__footer { gap: 8px 12px; } }
@media (prefers-reduced-motion: reduce) { .ops-pulse, .ops-pipeline__line, .ops-pipeline__runner, .ops-type-row__bar span, .ops-live-row__marker.is-active i, .ops-spin { animation: none !important; }.ops-list-enter-active, .ops-list-leave-active { transition: none; } }
</style>
