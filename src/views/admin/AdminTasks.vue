<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Ban, CheckCircle2, Hourglass, Percent, RotateCcw, Search, Activity, XCircle } from 'lucide-vue-next'
import Pager from '@/views/textformat/Pager.vue'
import AdminView from '@/components/admin/AdminView.vue'
import AdminSegmented from '@/components/admin/AdminSegmented.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import AdminDrawer from '@/components/admin/AdminDrawer.vue'
import AdminEmptyState from '@/components/admin/AdminEmptyState.vue'
import ChartCard from '@/components/admin/ChartCard.vue'
import KpiCard from '@/components/admin/KpiCard.vue'
import MeterBar from '@/components/admin/MeterBar.vue'
import AdminChart from '@/components/admin/charts/AdminChart.vue'
import { barOption, heatmapOption, timeSeriesOption } from '@/components/admin/charts/options'
import { GROUP_LABELS, GROUP_ORDER, type ChartTokens } from '@/components/admin/charts/tokens'
import Button from '@/components/ui/Button.vue'
import Input from '@/components/ui/Input.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import { errorMessage, useAdminLoader } from '@/composables/useAdminLoader'
import * as api from '@/api/admin'
import type { ListPagination } from '@/api/listPaging'
import { taskModuleLabel } from '@/utils/taskTypes'
import { compact, date, delta, elapsed, metricCount, percent, plain, relative, seconds, statusLabel, tone } from '@/utils/adminFormat'
import { series, throughputScale } from './series'

const { push: toast } = useToast()
const RANGES = [['24h', '24 小时'], ['7d', '7 天'], ['30d', '30 天']] as const
const range = ref<api.ThroughputRange>('24h')
const PAGE_SIZE = 50

const metrics = ref<api.TaskMetrics | null>(null)
const throughput = ref<api.Throughput | null>(null)
const tasks = ref<api.AdminTask[]>([])
const pagination = ref<ListPagination>()
const filters = ref<api.TaskFilters>({ status: 'all', search: '', group: 'all', task_type: 'all' })
const searchDraft = ref('')
const page = ref(1)
const selected = ref(new Set<string>())
const detail = ref<api.TaskDetail | null>(null)
const detailLoading = ref(false)

const loader = useAdminLoader(async (signal) => {
  const [taskMetrics, flow, rows] = await Promise.all([
    api.getTaskMetrics(), api.getThroughput(range.value, signal), api.taskPage(filters.value, page.value, signal),
  ])
  return () => {
    metrics.value = taskMetrics; throughput.value = flow; tasks.value = rows.items; pagination.value = rows.pagination
    const pages = Math.max(1, Math.ceil(rows.pagination.total / rows.pagination.page_size))
    if (page.value > pages) page.value = pages
    const visible = new Set(rows.items.map(row => row.id))
    selected.value = new Set([...selected.value].filter(id => visible.has(id)))
  }
}, { poll: true, onActionError: message => toast({ title: '操作未完成', description: message, variant: 'destructive' }) })

watch(range, () => { void loader.load() })
watch(page, () => { void loader.load() })
watch(filters, () => { if (page.value !== 1) page.value = 1; else void loader.load() }, { deep: true })
// `?tab=tasks&search=<id>` deep-links from the logs drawer.
const route = useRoute()
watch(() => route.query.search, value => {
  if (typeof value === 'string' && value !== filters.value.search) { searchDraft.value = value; filters.value.search = value }
}, { immediate: true })
function applySearch() { filters.value.search = searchDraft.value.trim() }
function clearFilters() { searchDraft.value = ''; filters.value = { status: 'all', search: '', group: 'all', task_type: 'all' } }
const filtered = computed(() => Object.entries(filters.value).some(([key, value]) => key === 'search' ? !!value : value !== 'all'))

const counts = computed(() => metrics.value?.status_counts ?? {})
const totals = computed(() => throughput.value?.totals)
const prev = computed(() => throughput.value?.previous_totals)
const successRate = computed(() => {
  const done = (totals.value?.succeeded ?? 0) + (totals.value?.failed ?? 0)
  return done ? totals.value!.succeeded / done * 100 : null
})
const rangeLabel = computed(() => RANGES.find(([value]) => value === range.value)?.[1] ?? '')
const scale = computed(() => throughputScale(throughput.value?.range))
const taskTypes = computed(() => (metrics.value?.by_type ?? []).map(row => row.task_type).sort())
const byType = computed(() => (throughput.value?.by_type ?? []).slice(0, 10))

const flowChart = computed(() => (t: ChartTokens) => timeSeriesOption(t, {
  scale: scale.value, format: compact,
  series: [
    { name: '提交', color: t.series[0], area: true, data: series(throughput.value?.points, 'submitted') },
    { name: '完成', color: t.series[2], data: series(throughput.value?.points, 'succeeded') },
    { name: '失败', color: t.series[7], data: series(throughput.value?.points, 'failed') },
  ],
}))
const errorChart = computed(() => (t: ChartTokens) => {
  const rows = throughput.value?.top_errors ?? []
  return barOption(t, { horizontal: true, labels: true, categories: rows.map(row => row.code || '未分类'), format: compact,
    series: [{ name: '失败次数', color: t.status.critical, data: rows.map(row => row.count) }] })
})
const durationChart = computed(() => (t: ChartTokens) => barOption(t, {
  horizontal: true, categories: byType.value.map(row => row.task_type), format: value => seconds(value),
  series: [
    { name: '排队 P50', color: t.series[3], data: byType.value.map(row => row.wait_p50_s) },
    { name: '执行 P50', color: t.series[0], data: byType.value.map(row => row.duration_p50_s) },
    { name: '执行 P95', color: t.series[1], data: byType.value.map(row => row.duration_p95_s) },
  ],
}))
const outcomeChart = computed(() => (t: ChartTokens) => barOption(t, {
  horizontal: true, categories: byType.value.map(row => row.task_type), format: compact,
  series: [
    { name: '成功', color: t.status.good, stack: 'outcome', data: byType.value.map(row => row.succeeded) },
    { name: '失败', color: t.status.critical, stack: 'outcome', data: byType.value.map(row => row.failed) },
    { name: '进行中/其他', color: t.axis, stack: 'outcome', data: byType.value.map(row => row.total - row.succeeded - row.failed) },
  ],
}))
const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const heatChart = computed(() => (t: ChartTokens) => heatmapOption(t, {
  xLabels: Array.from({ length: 24 }, (_, hour) => String(hour).padStart(2, '0')), yLabels: WEEKDAYS, format: compact,
  data: (throughput.value?.heatmap ?? []).flatMap((row, day) => row.map((value, hour) => [hour, day, value] as [number, number, number])),
}))
const typeRows = computed(() => byType.value.map(row => ({ ...row, label: taskModuleLabel(row.task_type) })))

const allSelected = computed(() => tasks.value.length > 0 && tasks.value.every(row => selected.value.has(row.id)))
function toggleAll() { selected.value = allSelected.value ? new Set() : new Set(tasks.value.map(row => row.id)) }
function toggle(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) next.delete(id); else next.add(id)
  selected.value = next
}
const ACTIVE = ['pending', 'queued', 'running', 'retrying', 'cancelling', 'paused']
const RETRYABLE = ['failed', 'timeout', 'cancelled']
const selectedRows = computed(() => tasks.value.filter(row => selected.value.has(row.id)))
const cancellable = computed(() => selectedRows.value.filter(row => ACTIVE.includes(row.status)).length)
const retryable = computed(() => selectedRows.value.filter(row => RETRYABLE.includes(row.status)).length)

async function bulk(action: 'cancel' | 'retry') {
  const ids = selectedRows.value.filter(row => (action === 'cancel' ? ACTIVE : RETRYABLE).includes(row.status)).map(row => row.id)
  if (!ids.length) return
  const verb = action === 'cancel' ? '取消' : '重新排队'
  if (!await showConfirm(`确认${verb}选中的 ${ids.length} 个任务？`, { title: `批量${verb}`, destructive: action === 'cancel' })) return
  await loader.runAction(async () => {
    const result = await api.bulkTasks(action, ids)
    const failed = result.results.filter(row => !row.ok)
    // ok without changed means the task already had the target state: not a new cancellation
    const unchanged = result.results.filter(row => row.ok && row.changed === false).length
    const changed = result.results.filter(row => row.ok && row.changed !== false).length
    const notes = [
      ...(failed.length ? [`${failed.length} 个跳过：${[...new Set(failed.map(row => row.reason))].join('；')}`] : []),
      ...(unchanged ? [`${unchanged} 个已是目标状态，未改动`] : []),
    ]
    selected.value = new Set()
    toast({ title: `已${verb} ${changed} 个任务`, description: notes.length ? notes.join('；') : undefined, variant: failed.length ? 'default' : 'success' })
    await loader.loadData()
  })
}

async function openDetail(task: api.AdminTask) {
  detailLoading.value = true
  detail.value = { ...task, attempts: [], events: [] } as unknown as api.TaskDetail
  try { detail.value = await api.getTaskDetail(task.id) }
  catch (cause) { toast({ title: '无法读取任务详情', description: errorMessage(cause), variant: 'destructive' }) }
  finally { detailLoading.value = false }
}
async function single(action: 'cancel' | 'retry') {
  const task = detail.value
  if (!task) return
  const verb = action === 'cancel' ? '取消' : '重新排队'
  if (!await showConfirm(action === 'cancel' ? `确认取消任务 ${task.id}？` : `将任务 ${task.id} 重新放入队列。确认重试？`, { title: `${verb}任务`, destructive: action === 'cancel' })) return
  await loader.runAction(async () => {
    if (action === 'cancel') await api.cancelTask(task.id); else await api.retryTask(task.id)
    detail.value = null
    toast({ title: action === 'cancel' ? '取消请求已提交' : '任务已重新排队', variant: 'success' })
    await loader.loadData()
  })
}
</script>

<template>
  <AdminView title="任务与队列" description="任务吞吐、耗时与失败分析；筛选、批量处理与追踪单个任务。" :loader="loader">
    <template #actions>
      <AdminSegmented v-model="range" :options="RANGES" label="统计范围" />
    </template>
    <template v-if="metrics && throughput">
      <div class="kpi-grid kpi-grid--5">
        <KpiCard label="排队中" :value="plain(metricCount(counts, 'pending', 'queued', 'retrying'))" :icon="Hourglass" :accent="3" hint="当前" />
        <KpiCard label="运行中" :value="plain(metricCount(counts, 'running', 'cancelling'))" :icon="Activity" :accent="0" hint="当前" />
        <KpiCard :label="`完成 · ${rangeLabel}`" :value="compact(totals?.succeeded)" :icon="CheckCircle2" :accent="2" :delta="delta(totals?.succeeded ?? 0, prev?.succeeded ?? 0)" up-is-good :trend="throughput.points.map(p => p.succeeded)" hint="较上一周期" />
        <KpiCard :label="`失败 · ${rangeLabel}`" :value="compact(totals?.failed)" :icon="XCircle" :accent="4" :delta="delta(totals?.failed ?? 0, prev?.failed ?? 0)" :up-is-good="false" :trend="throughput.points.map(p => p.failed)" :tone="(totals?.failed ?? 0) ? 'danger' : 'default'" hint="含超时" />
        <KpiCard label="成功率" :value="percent(successRate)" :icon="Percent" :accent="2" :hint="`提交 ${compact(totals?.submitted)} 个`" />
      </div>

      <div class="chart-grid">
        <ChartCard title="提交与完成趋势" :subtitle="`近 ${rangeLabel}`" :span="8"
                   :columns="[{ key: 'time', label: '时间', format: v => date(v) }, { key: 'submitted', label: '提交' }, { key: 'succeeded', label: '完成' }, { key: 'failed', label: '失败' }]" :rows="throughput.points">
          <AdminChart :option="flowChart" :height="260" label="任务提交与完成趋势" />
        </ChartCard>
        <ChartCard title="失败原因 Top 8" subtitle="按错误码聚合" :span="4"
                   :columns="[{ key: 'code', label: '错误码' }, { key: 'count', label: '次数' }, { key: 'sample', label: '示例' }]" :rows="throughput.top_errors">
          <AdminChart v-if="throughput.top_errors.length" :option="errorChart" :height="260" label="失败原因条形图" />
          <p v-else class="admin-empty">统计范围内没有失败任务</p>
        </ChartCard>
        <ChartCard title="各类型耗时" subtitle="排队等待与执行时长分位（最近 5000 条）" :span="6"
                   :columns="[{ key: 'task_type', label: '类型' }, { key: 'label', label: '模块' }, { key: 'wait_p50_s', label: '排队 P50', format: seconds }, { key: 'wait_p95_s', label: '排队 P95', format: seconds }, { key: 'duration_p50_s', label: '执行 P50', format: seconds }, { key: 'duration_p95_s', label: '执行 P95', format: seconds }]" :rows="typeRows">
          <AdminChart v-if="byType.length" :option="durationChart" :height="Math.max(200, byType.length * 40)" label="各任务类型耗时条形图" />
          <p v-else class="admin-empty">统计范围内没有任务</p>
        </ChartCard>
        <ChartCard title="各类型结果" subtitle="成功 / 失败 / 进行中" :span="6"
                   :columns="[{ key: 'task_type', label: '类型' }, { key: 'total', label: '总数' }, { key: 'succeeded', label: '成功' }, { key: 'failed', label: '失败' }, { key: 'success_rate', label: '成功率', format: v => v == null ? '—' : percent(v * 100) }]" :rows="typeRows">
          <AdminChart v-if="byType.length" :option="outcomeChart" :height="Math.max(200, byType.length * 40)" label="各任务类型结果条形图" />
          <p v-else class="admin-empty">统计范围内没有任务</p>
        </ChartCard>
        <ChartCard title="提交时段分布" subtitle="近 30 天 · 星期 × 小时" :span="12">
          <AdminChart :option="heatChart" :height="250" label="任务提交时段热力图" />
        </ChartCard>
      </div>

      <section class="table-panel">
        <div class="table-toolbar">
          <div class="controls">
            <select v-model="filters.status" aria-label="任务状态"><option value="all">全部状态</option><option value="queued">排队中</option><option value="running">运行中</option><option value="completed">已完成</option><option value="failed">失败 / 超时</option><option value="cancelled">已取消</option></select>
            <select v-model="filters.group" aria-label="服务组"><option value="all">全部服务组</option><option v-for="group in GROUP_ORDER" :key="group" :value="group">{{ GROUP_LABELS[group] }}</option></select>
            <select v-model="filters.task_type" aria-label="任务类型"><option value="all">全部类型</option><option v-for="name in taskTypes" :key="name" :value="name">{{ name }}</option></select>
            <form class="search-field" role="search" @submit.prevent="applySearch"><Search class="h-4 w-4" aria-hidden="true" /><Input v-model="searchDraft" aria-label="搜索任务" placeholder="任务 ID、项目 ID 或用户名" class="search" /></form>
            <Button v-if="filtered" variant="ghost" size="sm" @click="clearFilters">清空筛选</Button>
          </div>
          <div v-if="selected.size" class="bulk-bar" role="status">
            <span>已选 {{ selected.size }} 项</span>
            <Button variant="outline" size="sm" :disabled="!retryable || loader.actionBusy.value" @click="bulk('retry')"><RotateCcw class="h-4 w-4" />重新排队 {{ retryable || '' }}</Button>
            <Button variant="destructive" size="sm" :disabled="!cancellable || loader.actionBusy.value" @click="bulk('cancel')"><Ban class="h-4 w-4" />取消 {{ cancellable || '' }}</Button>
          </div>
        </div>
        <AdminTable table-class="wide-table task-table">
          <thead><tr>
            <th class="select-cell"><input type="checkbox" :checked="allSelected" :indeterminate="selected.size > 0 && !allSelected" aria-label="全选本页" @change="toggleAll"></th>
            <th>类型 / ID</th><th>用户</th><th>状态</th><th>进度</th><th>耗时</th><th>Worker</th><th>创建时间</th><th class="user-actions">操作</th>
          </tr></thead>
          <tbody>
            <tr v-for="task in tasks" :key="task.id" :aria-selected="selected.has(task.id)">
              <td class="select-cell"><input type="checkbox" :checked="selected.has(task.id)" :aria-label="`选择任务 ${task.id}`" @change="toggle(task.id)"></td>
              <td><strong>{{ task.task_type }}</strong><small class="mono">{{ task.id }}</small></td>
              <td>{{ task.owner_username || '—' }}<small v-if="task.group">{{ GROUP_LABELS[task.group] ?? task.group }}</small></td>
              <td><StatusPill :label="statusLabel(task.status)" :tone="tone(task.status)" /></td>
              <td class="meter-cell"><MeterBar :value="task.progress" :max="100" :label="`${task.progress}%`" /></td>
              <td>{{ elapsed(task.started_at, task.finished_at) }}</td>
              <td class="clip" :title="task.worker_id ?? ''">{{ task.worker_id || '—' }}</td>
              <td :title="date(task.created_at)">{{ relative(task.created_at) }}</td>
              <td class="user-actions"><Button variant="outline" size="sm" @click="openDetail(task)">详情</Button></td>
            </tr>
          </tbody>
        </AdminTable>
        <AdminEmptyState v-if="!tasks.length" title="没有匹配的任务" description="调整状态、服务组或搜索条件后重试。"><Button v-if="filtered" variant="outline" size="sm" @click="clearFilters">清空筛选</Button></AdminEmptyState>
        <Pager :page="page" :page-count="Math.max(1, Math.ceil((pagination?.total ?? 0) / PAGE_SIZE))" :total="pagination?.total ?? 0" :page-size="PAGE_SIZE" unit="条" @update:page="page = $event" />
      </section>

      <AdminDrawer v-if="detail" title="任务详情" @close="detail = null">
        <div class="drawer-stack" :aria-busy="detailLoading">
          <div class="drawer-hero">
            <div><strong>{{ detail.task_type }}</strong><p class="mono">{{ detail.id }}</p></div>
            <StatusPill :label="statusLabel(detail.status)" :tone="tone(detail.status)" />
          </div>
          <MeterBar :value="detail.progress" :max="100" :label="`进度 ${detail.progress}%`" />
          <dl class="detail-grid">
            <div><dt>用户</dt><dd>{{ detail.owner_username || '—' }}</dd></div>
            <div><dt>项目</dt><dd>{{ detail.project_name ?? detail.project_id ?? '—' }}</dd></div>
            <div><dt>服务组</dt><dd>{{ GROUP_LABELS[detail.group ?? ''] ?? detail.group ?? '—' }}</dd></div>
            <div><dt>尝试次数</dt><dd>{{ detail.attempts?.length ?? detail.attempt_no ?? 0 }}</dd></div>
            <div><dt>创建</dt><dd>{{ date(detail.created_at) }}</dd></div>
            <div><dt>开始</dt><dd>{{ date(detail.started_at) }}</dd></div>
            <div><dt>结束</dt><dd>{{ date(detail.finished_at) }}</dd></div>
            <div><dt>耗时</dt><dd>{{ elapsed(detail.started_at, detail.finished_at) }}</dd></div>
          </dl>
          <p v-if="detail.error_message" class="admin-error">{{ detail.error_code }} · {{ detail.error_message }}</p>
          <div class="controls">
            <Button v-if="ACTIVE.includes(detail.status)" :disabled="loader.actionBusy.value" variant="destructive" @click="single('cancel')"><Ban class="h-4 w-4" />取消任务</Button>
            <Button v-if="RETRYABLE.includes(detail.status)" :disabled="loader.actionBusy.value" variant="outline" @click="single('retry')"><RotateCcw class="h-4 w-4" />重新排队</Button>
          </div>
          <template v-if="detail.attempts?.length">
            <h3 class="drawer-heading">执行尝试</h3>
            <AdminTable table-class="compact-table"><thead><tr><th>#</th><th>Worker</th><th>状态</th><th>开始</th><th>耗时</th></tr></thead>
              <tbody><tr v-for="attempt in detail.attempts" :key="attempt.attempt_no"><td>{{ attempt.attempt_no }}</td><td class="clip mono" :title="attempt.worker_id">{{ attempt.worker_id || '—' }}</td><td><StatusPill :label="statusLabel(attempt.status)" :tone="tone(attempt.status)" /></td><td>{{ date(attempt.started_at) }}</td><td>{{ elapsed(attempt.started_at, attempt.finished_at) }}</td></tr></tbody>
            </AdminTable>
          </template>
          <template v-if="detail.events?.length">
            <h3 class="drawer-heading">事件时间线 <span class="admin-muted">最近 {{ detail.events.length }} 条</span></h3>
            <ol class="timeline">
              <li v-for="event in detail.events" :key="event.sequence">
                <span class="timeline__dot" aria-hidden="true" />
                <div><strong>{{ event.type }}</strong><small>{{ date(event.time) }}</small><p v-if="event.summary">{{ event.summary }}</p></div>
              </li>
            </ol>
          </template>
          <p v-else-if="detailLoading" class="admin-muted">正在读取尝试与事件…</p>
        </div>
      </AdminDrawer>
    </template>
  </AdminView>
</template>
