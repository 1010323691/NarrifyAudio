<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Boxes, Database, Gauge, HardDrive, Layers, ListOrdered, Network, Server, Timer, Zap } from 'lucide-vue-next'
import AdminView from '@/components/admin/AdminView.vue'
import AdminSegmented from '@/components/admin/AdminSegmented.vue'
import ChartCard from '@/components/admin/ChartCard.vue'
import KpiCard from '@/components/admin/KpiCard.vue'
import MeterBar from '@/components/admin/MeterBar.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import AdminChart from '@/components/admin/charts/AdminChart.vue'
import { barOption, donutOption, gaugeOption, timeSeriesOption, type TimeSeries } from '@/components/admin/charts/options'
import type { ChartTokens } from '@/components/admin/charts/tokens'
import StatusPill from '@/components/ui/StatusPill.vue'
import Pager from '@/views/textformat/Pager.vue'
import GpuScheduler from '@/components/settings/GpuScheduler.vue'
import { useAdminLoader } from '@/composables/useAdminLoader'
import * as api from '@/api/admin'
import type { ListPagination } from '@/api/listPaging'
import { bytes, compact, percent, plain, relative, seconds, statusLabel, tone } from '@/utils/adminFormat'
import { gpuIndexes, hasKey, scaleFor, series, throughputScale, values } from './series'

const RANGES = [['1h', '1 小时'], ['6h', '6 小时'], ['24h', '24 小时'], ['7d', '7 天']] as const
const SPANS: Record<api.HistoryRange, number> = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800 }
const API_MINUTES: Record<api.HistoryRange, number> = { '1h': 60, '6h': 360, '24h': 1440, '7d': 1440 }
const range = ref<api.HistoryRange>('1h')

const performance = ref<api.AdminPerformance | null>(null)
const history = ref<api.MetricsHistory | null>(null)
const apiSeries = ref<api.ApiSeries | null>(null)
const throughput = ref<api.Throughput | null>(null)

const loader = useAdminLoader(async (signal) => {
  const selected = range.value
  const [snapshot, samples, latency, flow, applyWorkers] = await Promise.all([
    api.getPerformance(), api.getMetricsHistory(selected, signal), api.getApiSeries(API_MINUTES[selected], signal),
    api.getThroughput(selected === '7d' ? '7d' : '24h', signal), applyWorkerPage(workerPage.value, signal),
  ])
  return () => { performance.value = snapshot; history.value = samples; apiSeries.value = latency; throughput.value = flow; applyWorkers?.() }
}, { poll: true })
watch(range, () => { void loader.load() })

const points = computed(() => history.value?.points ?? [])
const scale = computed(() => scaleFor(history.value?.step_seconds ?? 30, SPANS[range.value]))
const sampled = computed(() => (history.value?.sample_count ?? 0) > 0)
const db = computed(() => performance.value?.database ?? null)
const queue = computed(() => performance.value?.queue)
const latestDb = computed(() => [...points.value].reverse().find(point => typeof point.db_cache_hit_percent === 'number'))
const latestWriteRate = computed(() => {
  const point = [...points.value].reverse().find(row => typeof row.db_inserted_ps === 'number')
  return point ? ['db_inserted_ps', 'db_updated_ps', 'db_deleted_ps'].reduce((sum, key) => sum + (Number(point[key]) || 0), 0) : null
})
const writeTrend = computed(() => points.value.map(point => typeof point.db_inserted_ps === 'number'
  ? ['db_inserted_ps', 'db_updated_ps', 'db_deleted_ps'].reduce((sum, key) => sum + (Number(point[key]) || 0), 0) : null))
const workerPageSize = ref(10)
const workerPage = ref(1)
const workers = ref<api.WorkerStatus[]>([])
const workerPagination = ref<ListPagination>()
const workerTotal = computed(() => workerPagination.value?.total ?? 0)
const workerOnline = computed(() => workerPagination.value?.counts.online ?? 0)
const workerPageCount = computed(() => Math.max(1, Math.ceil(workerTotal.value / workerPageSize.value)))
// 页码翻页只拉当前页；ticket 保证慢响应不会覆盖更新的页。
let workerTicket = 0
async function applyWorkerPage(page: number, signal?: AbortSignal) {
  const ticket = ++workerTicket
  const rows = await api.workerPage(page, workerPageSize.value, signal)
  if (ticket !== workerTicket) return null
  return () => {
    const last = Math.max(1, Math.ceil(rows.pagination.total / workerPageSize.value))
    workers.value = rows.items; workerPagination.value = rows.pagination
    if (workerPage.value > last) workerPage.value = last
  }
}
function changeWorkerPageSize(size: number) { workerPageSize.value = size; if (workerPage.value !== 1) workerPage.value = 1; else void reloadWorkers() }
async function reloadWorkers() {
  try { (await applyWorkerPage(workerPage.value))?.() } catch { /* 下一次轮询会重试 */ }
}
watch(workerPage, async page => {
  try { (await applyWorkerPage(page))?.() } catch { /* 下一次轮询会重试 */ }
})

type Line = Omit<TimeSeries, 'data'> & { key: string }
function lines(definitions: (tokens: ChartTokens) => Line[], format: (value: number) => string, extra: { decimals?: boolean; max?: number; fit?: boolean } = {}) {
  return (tokens: ChartTokens) => timeSeriesOption(tokens, {
    scale: scale.value, format, ...extra,
    series: definitions(tokens).map(({ key, ...rest }) => ({ ...rest, data: series(points.value, key) })),
  })
}
const count = (value: number) => plain(value, 0)
const rate = (value: number) => `${compact(value)}/s`

const slotsChart = computed(() => lines(t => [
  { key: 'slots_active', name: '占用槽位', color: t.series[0], area: true },
  { key: 'slots_total', name: '总槽位', color: t.muted, dashed: true },
], count))
const queueChart = computed(() => lines(t => [
  { key: 'queue_length', name: 'Stream 长度', color: t.series[0] },
  { key: 'queue_pending', name: '待确认 (pending)', color: t.series[1] },
], count))
const ttsChart = computed(() => lines(t => [
  { key: 'tts_batch_active', name: '批内合成中', color: t.series[1], area: true },
  { key: 'tts_batch_parked', name: '已挂起', color: t.series[2] },
  { key: 'tts_batch_queued', name: '排队', color: t.series[3] },
], count))
const llmChart = computed(() => lines(t => [
  { key: 'llm_running', name: '运行中任务', color: t.series[0], area: true },
  { key: 'llm_gate_active', name: '占用 LLM 闸门', color: t.series[2] },
  { key: 'llm_queued', name: '排队', color: t.series[3] },
  { key: 'llm_limit', name: '闸门并发', color: t.muted, dashed: true },
], count))
const audioChart = computed(() => lines(t => [
  { key: 'audio_running', name: '运行中', color: t.series[2], area: true },
  { key: 'audio_queued', name: '排队', color: t.series[3] },
], count))

const flowPoints = computed(() => throughput.value?.points ?? [])
const flowScale = computed(() => throughputScale(throughput.value?.range))
const flowUnit = computed(() => (throughput.value?.step_seconds ?? 3600) >= 21600 ? '每 6 小时' : '每小时')
const ttsFlowChart = computed(() => (t: ChartTokens) => timeSeriesOption(t, {
  scale: flowScale.value, format: compact, series: [{ name: 'TTS 合成字数', color: t.series[1], type: 'bar', data: series(flowPoints.value, 'tts_chars') }],
}))
const llmFlowChart = computed(() => (t: ChartTokens) => timeSeriesOption(t, {
  scale: flowScale.value, format: compact, series: [{ name: 'LLM 输出字数', color: t.series[0], type: 'bar', data: series(flowPoints.value, 'llm_chars') }],
}))

const connChart = computed(() => lines(t => [
  { key: 'db_conn_active', name: '活跃', color: t.series[0], area: true, stack: 'conn' },
  { key: 'db_conn_idle_in_transaction', name: '事务中空闲', color: t.series[1], area: true, stack: 'conn' },
  { key: 'db_conn_idle', name: '空闲', color: t.series[2], area: true, stack: 'conn' },
], count))
const readChart = computed(() => lines(t => [
  { key: 'db_fetched_ps', name: '索引读取 (fetched)', color: t.series[0] },
  { key: 'db_returned_ps', name: '扫描返回 (returned)', color: t.series[1] },
], rate, { decimals: true }))
const writeChart = computed(() => lines(t => [
  { key: 'db_inserted_ps', name: '插入', color: t.series[0] },
  { key: 'db_updated_ps', name: '更新', color: t.series[1] },
  { key: 'db_deleted_ps', name: '删除', color: t.series[2] },
], rate, { decimals: true }))
const txChart = computed(() => lines(t => [
  { key: 'db_commit_ps', name: '提交', color: t.series[0] },
  { key: 'db_rollback_ps', name: '回滚', color: t.series[1] },
], rate, { decimals: true }))
const cacheChart = computed(() => lines(t => [{ key: 'db_cache_hit_percent', name: '缓存命中率', color: t.series[2] }], value => percent(value, 1), { decimals: true, max: 100, fit: true }))
const redisChart = computed(() => lines(t => [{ key: 'redis_ops_per_sec', name: 'Redis ops/s', color: t.series[4] }], rate, { decimals: true }))

const apiPoints = computed(() => apiSeries.value?.points ?? [])
const apiScale = computed(() => (apiSeries.value?.minutes ?? 60) <= 360 ? 'minute' as const : 'hour' as const)
const apiRequestChart = computed(() => (t: ChartTokens) => timeSeriesOption(t, {
  scale: apiScale.value, format: count,
  series: [
    { name: '全部请求', color: t.series[0], area: true, data: series(apiPoints.value, 'requests') },
    { name: '业务请求', color: t.series[1], data: series(apiPoints.value, 'business_requests') },
    { name: '5xx', color: t.series[7], data: series(apiPoints.value, 'errors') },
  ],
}))
const apiLatencyChart = computed(() => (t: ChartTokens) => timeSeriesOption(t, {
  scale: apiScale.value, format: value => `${plain(value, 0)} ms`, decimals: true,
  series: [
    { name: '平均', color: t.series[0], data: series(apiPoints.value, 'average_ms') },
    { name: 'P95', color: t.series[1], data: series(apiPoints.value, 'p95_ms') },
  ],
}))
const statusCodeChart = computed(() => (t: ChartTokens) => {
  const counts = performance.value?.api.status_counts ?? {}
  const bucket = (prefix: string) => Object.entries(counts).filter(([code]) => code.startsWith(prefix)).reduce((sum, [, value]) => sum + value, 0)
  return donutOption(t, {
    title: '近 5 分钟请求', format: count,
    data: [
      { name: '2xx', value: bucket('2'), color: t.status.good },
      { name: '3xx', value: bucket('3'), color: t.series[0] },
      { name: '4xx', value: bucket('4'), color: t.status.warning },
      { name: '5xx', value: bucket('5'), color: t.status.critical },
    ],
  })
})
const endpointChart = computed(() => (t: ChartTokens) => {
  const rows = performance.value?.api.endpoints ?? []
  return barOption(t, {
    horizontal: true, categories: rows.map(row => row.route), format: value => `${plain(value, 0)} ms`,
    series: [
      { name: '平均', color: t.series[0], data: rows.map(row => row.average_ms) },
      { name: 'P95', color: t.series[1], data: rows.map(row => row.p95_ms) },
    ],
  })
})
const stageChart = computed(() => (t: ChartTokens) => {
  const rows = performance.value?.api.tts_stages ?? []
  return barOption(t, { horizontal: true, labels: true, categories: rows.map(row => row.stage), format: value => `${plain(value, 0)} ms`,
    series: [{ name: 'P95', color: t.series[1], data: rows.map(row => row.p95_ms) }] })
})

const host = computed(() => performance.value?.system)
const hostGauges = computed(() => {
  const system = host.value
  if (!system) return []
  const rows: { key: string; label: string; value: number; detail: string }[] = []
  if (system.cpu_percent != null) rows.push({ key: 'cpu', label: 'CPU', value: system.cpu_percent, detail: system.load_average_1m != null ? `1 分钟负载 ${system.load_average_1m}` : '' })
  if (system.ram_used_bytes != null && system.ram_total_bytes) rows.push({ key: 'ram', label: '内存', value: system.ram_used_bytes / system.ram_total_bytes * 100, detail: `${bytes(system.ram_used_bytes)} / ${bytes(system.ram_total_bytes)}` })
  if (system.disk_used_bytes != null && system.disk_total_bytes) rows.push({ key: 'disk', label: '存储盘', value: system.disk_used_bytes / system.disk_total_bytes * 100, detail: `${bytes(system.disk_free_bytes)} 可用` })
  return rows.map(row => ({ ...row, option: (t: ChartTokens) => gaugeOption(t, { value: row.value, max: 100, label: row.label, format: value => percent(value, 0), warn: .75, critical: .9 }) }))
})
const gpus = computed(() => gpuIndexes(history.value?.points))
const gpuChart = computed(() => lines(t => gpus.value.flatMap((index, order) => [
  { key: `gpu${index}_util`, name: `GPU${index} 利用率`, color: t.series[order * 2 % 8] },
  { key: `gpu${index}_mem_percent`, name: `GPU${index} 显存`, color: t.series[(order * 2 + 1) % 8] },
]), value => percent(value, 0), { max: 100 }))
const hostChart = computed(() => lines(t => [
  { key: 'cpu_percent', name: 'CPU', color: t.series[0] },
  { key: 'ram_percent', name: '内存', color: t.series[1] },
], value => percent(value, 0), { max: 100 }))

function workerSlots(worker: api.WorkerStatus) {
  return { active: Number(worker.capabilities.active_slots ?? (worker.current_task_id ? 1 : 0)), total: Number(worker.capabilities.slots ?? 1) }
}
function workerPool(worker: api.WorkerStatus): api.DbPool | null {
  return (worker.capabilities.db_pool as api.DbPool | null | undefined) ?? null
}
function workerTypes(worker: api.WorkerStatus): string {
  return Array.isArray(worker.capabilities.task_types) ? (worker.capabilities.task_types as string[]).join(' · ') : '—'
}
</script>

<template>
  <AdminView title="系统监控" description="Worker、数据库、队列、TTS / LLM 与 API 的资源使用和趋势。" :loader="loader">
    <template #actions>
      <AdminSegmented v-model="range" :options="RANGES" label="时间范围" />
    </template>
    <template v-if="performance">
      <p v-if="!sampled" class="admin-notice">尚无历史采样：趋势图由 Worker 每 30 秒采样一次并落库。启动 Worker 并等待一个采样周期后即可显示；实时快照不受影响。</p>

      <h2 class="section-title"><Boxes class="h-4 w-4" />Worker 与队列</h2>
      <div class="kpi-grid kpi-grid--5">
        <KpiCard label="在线 Worker" :value="plain(performance.tasks.worker_pool.online_workers)" :icon="Server" :accent="0" :hint="`共登记 ${workerTotal} 个`" />
        <KpiCard label="槽位占用" :value="`${performance.tasks.worker_pool.active_slots} / ${performance.tasks.worker_pool.total_slots}`" :icon="Layers" :accent="2" :trend="values(points, 'slots_active')" :hint="`${performance.tasks.worker_pool.idle_slots} 个空闲`" />
        <KpiCard label="队列长度" :value="queue?.available ? plain(queue.length) : '不可用'" :icon="ListOrdered" :accent="3" :trend="values(points, 'queue_length')" :tone="queue?.available ? 'default' : 'danger'" :hint="queue?.available ? `${queue.pending} 条待确认` : queue?.error" />
        <KpiCard label="Redis 吞吐" :value="queue?.ops_per_sec != null ? `${plain(queue.ops_per_sec)}/s` : '—'" :icon="Zap" :accent="4" :trend="values(points, 'redis_ops_per_sec')" :hint="queue?.used_memory_bytes != null ? `内存 ${bytes(queue.used_memory_bytes)} · ${queue.connected_clients} 个连接` : ''" />
        <KpiCard label="近 60 秒吞吐" :value="`${performance.tasks.throughput_60s.completed} 完成`" :icon="Timer" :accent="1" :hint="`提交 ${performance.tasks.throughput_60s.submitted} · 开始 ${performance.tasks.throughput_60s.started} · 失败 ${performance.tasks.throughput_60s.failed}`" />
      </div>
      <div class="chart-grid">
        <ChartCard title="Worker 槽位占用" subtitle="所有在线 Worker 的活跃槽位 / 总槽位" :hint="history?.scope" :span="6">
          <AdminChart :option="slotsChart" :height="220" label="Worker 槽位占用趋势" />
        </ChartCard>
        <ChartCard title="任务队列积压" subtitle="Redis Stream 长度与已投递未确认消息" :span="6">
          <AdminChart :option="queueChart" :height="220" label="任务队列积压趋势" />
        </ChartCard>
        <ChartCard title="Worker 明细" :subtitle="`${workerOnline} 个在线 · 槽位与各进程数据库连接池`" :span="12">
          <AdminTable table-class="wide-table worker-table" :page-size="workerPageSize">
            <thead><tr><th>Worker</th><th>状态</th><th>通道</th><th>槽位</th><th>DB 连接池</th><th>任务类型</th><th>最近心跳</th></tr></thead>
            <tbody>
              <tr v-for="worker in workers" :key="worker.worker_id">
                <td class="mono">{{ worker.worker_id }}</td>
                <td><StatusPill :label="statusLabel(worker.status)" :tone="tone(worker.status)" /></td>
                <td>{{ worker.capabilities.task_lane ?? '—' }}</td>
                <td class="meter-cell"><MeterBar :value="workerSlots(worker).active" :max="workerSlots(worker).total" :label="`${workerSlots(worker).active}/${workerSlots(worker).total}`" :warn=".8" /></td>
                <td class="meter-cell"><MeterBar v-if="workerPool(worker)" :value="workerPool(worker)!.checked_out" :max="workerPool(worker)!.size + workerPool(worker)!.max_overflow" :label="`${workerPool(worker)!.checked_out}/${workerPool(worker)!.size}`" :warn=".8" :critical="1" /><span v-else class="admin-muted">—</span></td>
                <td class="clip" :title="workerTypes(worker)">{{ workerTypes(worker) }}</td>
                <td>{{ relative(worker.last_seen_at) }}</td>
              </tr>
              <tr v-if="!workers.length"><td colspan="7" class="admin-empty-cell">暂无 Worker 心跳</td></tr>
            </tbody>
          </AdminTable>
          <Pager :page="workerPage" :page-count="workerPageCount" :total="workerTotal" :page-size="workerPageSize" unit="个 Worker" @update:page="workerPage = $event" @update:page-size="changeWorkerPageSize" />
        </ChartCard>
      </div>

      <h2 class="section-title"><Gauge class="h-4 w-4" />TTS / LLM / 音频</h2>
      <div class="chart-grid">
        <ChartCard title="TTS 批内并发与排队" :subtitle="`tts.batch 成员（章节）· 执行池 ${performance.tts_batch.pools} 个，批内合成中 ${performance.tts_batch.active_members}，已挂起 ${performance.tts_batch.parked_members}，排队 ${performance.tts_batch.queued_members}`" :span="4">
          <AdminChart :option="ttsChart" :height="220" label="TTS 并发与排队趋势" />
        </ChartCard>
        <ChartCard title="LLM 并发与排队" :subtitle="`script.* / music.* 任务 · 任务容量 ${performance.llm_task_limit}（含机械阶段，可插队）· 闸门并发 ${performance.llm_limit}`" :span="4">
          <AdminChart :option="llmChart" :height="220" label="LLM 并发与排队趋势" />
        </ChartCard>
        <ChartCard title="音频处理并发" subtitle="合并 / 混音 (FFmpeg)" :span="4">
          <AdminChart :option="audioChart" :height="220" label="音频处理并发趋势" />
        </ChartCard>
        <ChartCard title="TTS 合成吞吐" :subtitle="`${flowUnit}输入字数（额度账本实耗）`" :span="6"
                   :columns="[{ key: 'time', label: '时间', format: v => new Date(v).toLocaleString('zh-CN', { hour12: false }) }, { key: 'tts_chars', label: 'TTS 字数' }, { key: 'llm_chars', label: 'LLM 字数' }]" :rows="flowPoints">
          <AdminChart :option="ttsFlowChart" :height="220" label="TTS 合成吞吐柱状图" />
        </ChartCard>
        <ChartCard title="LLM 输出吞吐" :subtitle="`${flowUnit}输出字数（额度账本实耗）`" :span="6">
          <AdminChart :option="llmFlowChart" :height="220" label="LLM 输出吞吐柱状图" />
        </ChartCard>
        <ChartCard v-if="performance.api.tts_stages?.length" title="TTS 阶段耗时 P95" subtitle="API 进程近 5 分钟" :span="6">
          <AdminChart :option="stageChart" :height="200" label="TTS 阶段耗时条形图" />
        </ChartCard>
        <div :class="performance.api.tts_stages?.length ? 'span-6' : 'span-12'"><GpuScheduler mode="status" /></div>
      </div>

      <h2 class="section-title"><Database class="h-4 w-4" />数据库</h2>
      <p v-if="!db" class="admin-notice">当前数据库不是 PostgreSQL，没有实时连接与读写统计{{ hasKey(points, 'db_conn_total') ? '；下方为已采样的历史数据' : '' }}。</p>
      <template v-if="db || hasKey(points, 'db_conn_total')">
        <div v-if="db" class="kpi-grid kpi-grid--5">
          <KpiCard label="连接数" :value="`${db.connections.total} / ${db.connections.max}`" :icon="Network" :accent="0" :trend="values(points, 'db_conn_total')" :hint="`活跃 ${db.connections.active} · 事务中空闲 ${db.connections.idle_in_transaction}`" :tone="db.connections.total / db.connections.max > .8 ? 'warning' : 'default'" />
          <KpiCard label="API 连接池" :value="performance.api_pool ? `${performance.api_pool.checked_out} / ${performance.api_pool.size}` : '—'" :icon="Layers" :accent="2" :hint="performance.api_pool ? `溢出 ${performance.api_pool.overflow} / ${performance.api_pool.max_overflow}` : '无连接池 (SQLite)'" />
          <KpiCard label="缓存命中率" :value="percent(latestDb?.db_cache_hit_percent as number | undefined, 2)" :icon="Gauge" :accent="2" :trend="values(points, 'db_cache_hit_percent')" hint="共享缓冲区命中" />
          <KpiCard label="写入速率" :value="latestWriteRate == null ? '—' : rate(latestWriteRate)" :icon="Zap" :accent="1" :trend="writeTrend" hint="插入+更新+删除 行/秒" />
          <KpiCard label="数据库大小" :value="bytes(db.size_bytes)" :icon="HardDrive" :accent="6" :trend="values(points, 'db_size_bytes')" :hint="`累计提交 ${compact(db.counters.xact_commit)} 次`" />
        </div>
        <div class="chart-grid">
          <ChartCard title="连接数" :subtitle="`按状态堆叠${db ? ` · 上限 ${db.connections.max}` : ''}`" :span="6">
            <AdminChart :option="connChart" :height="220" label="数据库连接数趋势" />
          </ChartCard>
          <ChartCard title="事务速率" subtitle="每秒提交 / 回滚" :span="6">
            <AdminChart :option="txChart" :height="220" label="数据库事务速率趋势" />
          </ChartCard>
          <ChartCard title="读取速率" subtitle="每秒读取行数" :span="4">
            <AdminChart :option="readChart" :height="220" label="数据库读取速率趋势" />
          </ChartCard>
          <ChartCard title="写入 / 删除速率" subtitle="每秒插入、更新、删除行数" :span="4">
            <AdminChart :option="writeChart" :height="220" label="数据库写入删除速率趋势" />
          </ChartCard>
          <ChartCard title="缓存命中率" subtitle="blks_hit / (hit + read)" :span="4">
            <AdminChart :option="cacheChart" :height="220" label="数据库缓存命中率趋势" />
          </ChartCard>
        </div>
      </template>
      <div v-if="hasKey(points, 'redis_ops_per_sec')" class="chart-grid">
        <ChartCard title="Redis 操作速率" subtitle="instantaneous_ops_per_sec" :span="12">
          <AdminChart :option="redisChart" :height="180" label="Redis 操作速率趋势" />
        </ChartCard>
      </div>

      <h2 class="section-title"><Network class="h-4 w-4" />API</h2>
      <div class="chart-grid">
        <ChartCard title="请求量" :subtitle="`每 ${apiSeries?.step_minutes ?? 1} 分钟请求数`" :hint="apiSeries?.scope" :span="4">
          <AdminChart :option="apiRequestChart" :height="220" label="API 请求量趋势" />
        </ChartCard>
        <ChartCard title="响应延迟" subtitle="平均与 P95（分桶近似）" :hint="apiSeries?.scope" :span="4">
          <AdminChart :option="apiLatencyChart" :height="220" label="API 响应延迟趋势" />
        </ChartCard>
        <ChartCard title="状态码分布" :subtitle="`错误率 ${percent(performance.api.error_rate * 100, 2)} · P95 ${performance.api.p95_ms ?? '—'} ms`" :span="4">
          <AdminChart :option="statusCodeChart" :height="220" label="API 状态码分布环形图" />
        </ChartCard>
        <ChartCard title="端点延迟 Top 8" :subtitle="performance.api.scope" :span="12"
                   :columns="[{ key: 'route', label: '路由' }, { key: 'requests', label: '请求' }, { key: 'server_errors', label: '5xx' }, { key: 'average_ms', label: '平均 ms' }, { key: 'p95_ms', label: 'P95 ms' }]" :rows="performance.api.endpoints">
          <AdminChart v-if="performance.api.endpoints.length" :option="endpointChart" :height="Math.max(160, performance.api.endpoints.length * 34)" label="端点延迟条形图" />
          <p v-else class="admin-empty">此时间窗口暂无请求样本</p>
        </ChartCard>
      </div>

      <details v-if="hostGauges.length || gpus.length || performance.gpu.length" class="host-details">
        <summary><HardDrive class="h-4 w-4" />主机资源<span class="admin-muted">（尽力采集，仅供参考{{ host?.uptime_seconds != null ? ` · 已运行 ${seconds(host.uptime_seconds)}` : '' }}）</span></summary>
        <div class="chart-grid">
          <ChartCard v-if="hostGauges.length" title="当前负载" :span="6">
            <div class="gauge-grid">
              <div v-for="gauge in hostGauges" :key="gauge.key" class="gauge-cell">
                <AdminChart :option="gauge.option" :height="140" :label="`${gauge.label} 仪表`" />
                <small>{{ gauge.detail }}</small>
              </div>
            </div>
          </ChartCard>
          <ChartCard v-if="hasKey(points, 'cpu_percent')" title="CPU / 内存" :span="6">
            <AdminChart :option="hostChart" :height="200" label="CPU 与内存趋势" />
          </ChartCard>
          <ChartCard v-if="gpus.length" title="GPU 利用率与显存" :span="12">
            <AdminChart :option="gpuChart" :height="220" label="GPU 利用率与显存趋势" />
          </ChartCard>
          <ChartCard v-for="gpu in performance.gpu" :key="gpu.index" :title="`GPU ${gpu.index} · ${gpu.name}`" :span="4">
            <div class="kv-list">
              <p><span>利用率</span><strong>{{ percent(gpu.utilization_percent, 0) }}</strong></p>
              <p><span>显存</span><strong>{{ gpu.memory_used_mb == null ? '—' : `${plain(gpu.memory_used_mb)} / ${plain(gpu.memory_total_mb)} MB` }}</strong></p>
              <p><span>温度 / 功耗</span><strong>{{ gpu.temperature_c == null ? '—' : `${gpu.temperature_c} °C` }} / {{ gpu.power_w == null ? '—' : `${gpu.power_w} W` }}</strong></p>
            </div>
          </ChartCard>
        </div>
      </details>
    </template>
  </AdminView>
</template>
