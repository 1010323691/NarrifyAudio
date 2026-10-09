<script setup lang="ts">
import { computed, ref } from 'vue'
import { Activity, AlertTriangle, ArrowUpRight, CheckCircle2, Clock3, Cpu, Database, FileAudio, Hourglass, MessageSquareText, Users, XCircle } from 'lucide-vue-next'
import AdminView from '@/components/admin/AdminView.vue'
import ChartCard from '@/components/admin/ChartCard.vue'
import KpiCard from '@/components/admin/KpiCard.vue'
import AdminChart from '@/components/admin/charts/AdminChart.vue'
import { barOption, donutOption, gaugeOption } from '@/components/admin/charts/options'
import { GROUP_LABELS, GROUP_ORDER, groupColor, type ChartTokens } from '@/components/admin/charts/tokens'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useAdminLoader } from '@/composables/useAdminLoader'
import * as api from '@/api/admin'
import { compact, delta, metricCount, percent, plain, relative, statusLabel, tone } from '@/utils/adminFormat'
import { summed, values } from './series'

const overview = ref<api.AdminOverview | null>(null)
const metrics = ref<api.TaskMetrics | null>(null)
const throughput = ref<api.Throughput | null>(null)
const history = ref<api.MetricsHistory | null>(null)

const loader = useAdminLoader(async (signal) => {
  const [current, taskMetrics, flow, samples] = await Promise.all([
    api.getOverview(), api.getTaskMetrics(), api.getThroughput('24h', signal),
    api.getMetricsHistory('24h', signal).catch(() => null),
  ])
  return () => { overview.value = current; metrics.value = taskMetrics; throughput.value = flow; history.value = samples }
}, { poll: true })

const health = computed(() => {
  const services = overview.value?.services ?? []
  const errors = services.filter(item => item.status === 'error').length
  const warnings = services.filter(item => item.status === 'warning').length
  if (errors) return { tone: 'negative' as const, title: `${errors} 项服务异常`, text: '请优先处理标记为异常的服务。' }
  if (warnings) return { tone: 'warning' as const, title: `${warnings} 项服务需要关注`, text: '部分 Worker 或依赖未就绪，相关任务可能排队。' }
  return { tone: 'positive' as const, title: '所有服务运行正常', text: `${services.length} 项服务检查全部通过。` }
})

const totals = computed(() => throughput.value?.totals)
const previous = computed(() => throughput.value?.previous_totals)
const hourly = computed(() => throughput.value?.points ?? [])
const runningTrend = computed(() => summed(history.value?.points, GROUP_ORDER.map(group => `${group}_running`)).map(([, value]) => value))
const queuedTrend = computed(() => summed(history.value?.points, GROUP_ORDER.map(group => `${group}_queued`)).map(([, value]) => value))
const successRate = computed(() => {
  const done = (totals.value?.succeeded ?? 0) + (totals.value?.failed ?? 0)
  return done ? (totals.value!.succeeded / done) * 100 : null
})

const throughputChart = computed(() => (tokens: ChartTokens) => barOption(tokens, {
  categories: hourly.value.map(point => `${new Date(point.time).getHours().toString().padStart(2, '0')}:00`),
  series: GROUP_ORDER.filter(group => hourly.value.some(point => Number(point[`${group}_submitted`]) > 0)).map(group => ({
    name: GROUP_LABELS[group], color: groupColor(tokens, group), stack: 'submitted',
    data: hourly.value.map(point => Number(point[`${group}_submitted`]) || 0),
  })),
  format: value => compact(value),
}))
const throughputRows = computed(() => hourly.value.map(point => ({
  time: new Date(point.time).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }),
  submitted: point.submitted, succeeded: point.succeeded, failed: point.failed,
})))

const statusChart = computed(() => (tokens: ChartTokens) => {
  const counts = metrics.value?.status_counts ?? {}
  return donutOption(tokens, {
    title: '任务总数',
    format: value => compact(value),
    data: [
      { name: '运行中', value: metricCount(counts, 'running', 'cancelling'), color: tokens.series[0] },
      { name: '排队中', value: metricCount(counts, 'pending', 'queued', 'retrying'), color: tokens.status.warning },
      { name: '已完成', value: metricCount(counts, 'succeeded'), color: tokens.status.good },
      { name: '失败/超时', value: metricCount(counts, 'failed', 'timeout'), color: tokens.status.critical },
      { name: '已取消', value: metricCount(counts, 'cancelled', 'paused'), color: tokens.axis },
    ],
  })
})

const gauges = computed(() => {
  const data = overview.value
  if (!data) return []
  const rows = [
    { key: 'slots', label: 'Worker 槽位', value: data.workers.active_slots, max: data.workers.total_slots, format: (v: number) => `${v}/${data.workers.total_slots}` },
  ]
  if (data.database) rows.push({ key: 'db', label: '数据库连接', value: data.database.total, max: data.database.max, format: (v: number) => `${v}/${data.database!.max}` })
  if (data.system.cpu_percent != null) rows.push({ key: 'cpu', label: 'CPU', value: data.system.cpu_percent, max: 100, format: (v: number) => percent(v, 0) })
  if (data.system.ram_used_bytes != null && data.system.ram_total_bytes) {
    rows.push({ key: 'ram', label: '内存', value: data.system.ram_used_bytes / data.system.ram_total_bytes * 100, max: 100, format: (v: number) => percent(v, 0) })
  }
  return rows.map(row => ({ ...row, option: (tokens: ChartTokens) => gaugeOption(tokens, { value: row.value, max: row.max, label: row.label, format: row.format, warn: .75, critical: .9 }) }))
})
</script>

<template>
  <AdminView title="系统总览" description="平台健康、任务负载与资源使用的实时概况。" :loader="loader">
    <template v-if="overview && metrics">
      <div class="health-banner" :class="`is-${health.tone}`">
        <div class="health-banner__summary">
          <CheckCircle2 v-if="health.tone === 'positive'" class="h-5 w-5" /><AlertTriangle v-else class="h-5 w-5" />
          <div><strong>{{ health.title }}</strong><p>{{ health.text }}</p></div>
        </div>
        <ul class="health-banner__services">
          <li v-for="service in overview.services" :key="service.key" :title="service.detail">
            <span class="status-dot" :class="`is-${tone(service.status)}`" aria-hidden="true" />{{ service.name }}
            <span class="sr-only">：{{ statusLabel(service.status) }}，{{ service.detail }}</span>
          </li>
        </ul>
      </div>

      <div class="kpi-grid">
        <KpiCard label="运行中任务" :value="plain(overview.tasks.running)" :icon="Activity" :accent="0" :trend="runningTrend" hint="Worker 正在消费" />
        <KpiCard label="排队任务" :value="plain(overview.tasks.queued)" :icon="Hourglass" :accent="3" :trend="queuedTrend" hint="等待 Worker 领取" />
        <KpiCard label="近 24 小时完成" :value="compact(totals?.succeeded)" :icon="CheckCircle2" :accent="2" :delta="delta(totals?.succeeded ?? 0, previous?.succeeded ?? 0)" up-is-good :trend="hourly.map(p => p.succeeded)" :hint="successRate == null ? '' : `成功率 ${percent(successRate)}`" />
        <KpiCard label="近 24 小时失败" :value="compact(totals?.failed)" :icon="XCircle" :accent="4" :delta="delta(totals?.failed ?? 0, previous?.failed ?? 0)" :up-is-good="false" :trend="hourly.map(p => p.failed)" :tone="(totals?.failed ?? 0) > 0 ? 'danger' : 'default'" hint="较前 24 小时" />
        <KpiCard label="TTS 合成字数 · 24h" :value="compact(totals?.tts_chars)" :icon="FileAudio" :accent="1" :delta="delta(totals?.tts_chars ?? 0, previous?.tts_chars ?? 0)" up-is-good :trend="hourly.map(p => p.tts_chars)" :hint="`今日 ${compact(overview.today.tts_chars)}`" />
        <KpiCard label="LLM 输出字数 · 24h" :value="compact(totals?.llm_chars)" :icon="MessageSquareText" :accent="0" :delta="delta(totals?.llm_chars ?? 0, previous?.llm_chars ?? 0)" up-is-good :trend="hourly.map(p => p.llm_chars)" :hint="`今日 ${compact(overview.today.llm_chars)}`" />
        <KpiCard label="活跃用户 · 15 分钟" :value="plain(overview.today.active_users)" :icon="Users" :accent="6" :hint="`共 ${overview.user_count} 位用户`" />
        <KpiCard label="Worker 槽位占用" :value="`${overview.workers.active_slots} / ${overview.workers.total_slots}`" :icon="Cpu" :accent="2" :trend="values(history?.points, 'slots_active')" :hint="`${overview.workers.online_workers} 个 Worker 在线`" />
      </div>

      <div class="chart-grid">
        <ChartCard title="任务提交量 · 近 24 小时" subtitle="按服务组堆叠，每小时一柱" :span="8"
                   :columns="[{ key: 'time', label: '时间' }, { key: 'submitted', label: '提交' }, { key: 'succeeded', label: '完成' }, { key: 'failed', label: '失败' }]" :rows="throughputRows">
          <AdminChart :option="throughputChart" :height="260" label="近 24 小时按服务组的任务提交量柱状图" />
        </ChartCard>
        <ChartCard title="任务状态分布" subtitle="全部持久化任务" :span="4">
          <AdminChart :option="statusChart" :height="260" label="任务状态分布环形图" />
        </ChartCard>
        <ChartCard title="资源占用" subtitle="Worker 槽位、数据库连接与主机负载" :span="6">
          <div class="gauge-grid">
            <AdminChart v-for="gauge in gauges" :key="gauge.key" :option="gauge.option" :height="140" :label="`${gauge.label} 仪表`" />
          </div>
          <template #footer>
            <RouterLink to="/admin?tab=performance">打开系统监控 <ArrowUpRight class="h-3.5 w-3.5" /></RouterLink>
          </template>
        </ChartCard>
        <ChartCard title="最近异常" subtitle="失败任务与 API 5xx" :span="6">
          <ul v-if="overview.recent_errors.length" class="event-list">
            <li v-for="entry in overview.recent_errors" :key="entry.id">
              <span class="status-dot is-negative" aria-hidden="true" />
              <div><strong>{{ entry.type }}</strong><p :title="entry.message">{{ entry.message }}</p></div>
              <span class="event-list__meta"><StatusPill :label="entry.module" /><small><Clock3 class="h-3 w-3" />{{ relative(entry.time) }}</small></span>
            </li>
          </ul>
          <p v-else class="admin-empty">暂无近期异常记录</p>
          <template #footer>
            <span class="admin-muted"><Database class="inline h-3.5 w-3.5" /> 今日 API 请求 {{ compact(overview.today.api_requests) }}（{{ overview.today.api_requests_scope }}）</span>
            <RouterLink to="/admin?tab=logs">日志与异常 <ArrowUpRight class="h-3.5 w-3.5" /></RouterLink>
          </template>
        </ChartCard>
      </div>
    </template>
  </AdminView>
</template>
