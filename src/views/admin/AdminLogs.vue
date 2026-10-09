<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Download, Search } from 'lucide-vue-next'
import Pager from '@/views/textformat/Pager.vue'
import AdminView from '@/components/admin/AdminView.vue'
import AdminSegmented from '@/components/admin/AdminSegmented.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import AdminDrawer from '@/components/admin/AdminDrawer.vue'
import AdminEmptyState from '@/components/admin/AdminEmptyState.vue'
import ChartCard from '@/components/admin/ChartCard.vue'
import AdminChart from '@/components/admin/charts/AdminChart.vue'
import { donutOption, timeSeriesOption } from '@/components/admin/charts/options'
import type { ChartTokens } from '@/components/admin/charts/tokens'
import Input from '@/components/ui/Input.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { errorMessage, useAdminLoader } from '@/composables/useAdminLoader'
import * as api from '@/api/admin'
import type { ListPagination } from '@/api/listPaging'
import { compact, date, plain, relative } from '@/utils/adminFormat'
import { series } from './series'

const HOURS = [[1, '1 小时'], [24, '24 小时'], [168, '7 天'], [720, '30 天']] as const
const MODULES = ['system', 'api', 'llm', 'tts', 'audio', 'worker']
const MODULE_LABELS: Record<string, string> = { system: '系统 / 审计', api: 'API', llm: 'LLM', tts: 'TTS', audio: '音频', worker: '其他' }
const PAGE_SIZE = 50

const logHours = ref<number>(24)
const logLevel = ref('all')
const logModule = ref('all')
const logSearch = ref('')
const searchDraft = ref('')
const eventPage = ref(1)
const events = ref<api.AdminEvent[]>([])
const pagination = ref<ListPagination>()
const stats = ref<api.EventStats | null>(null)
const selected = ref<api.AdminEvent | null>(null)
const audit = ref<api.AuditDetail | null>(null)
const auditError = ref('')

const loader = useAdminLoader(async (signal) => {
  const [rows, distribution] = await Promise.all([
    api.eventPage(logLevel.value, logModule.value, logSearch.value, logHours.value, eventPage.value, signal),
    api.getEventStats(logLevel.value, logModule.value, logSearch.value, logHours.value, signal),
  ])
  return () => {
    events.value = rows.items; pagination.value = rows.pagination; stats.value = distribution
    if (rows.pagination) eventPage.value = Math.min(eventPage.value, Math.max(1, Math.ceil(rows.pagination.total / rows.pagination.page_size)))
  }
})
watch([logLevel, logModule, logHours, logSearch], () => { eventPage.value = 1 })
watch([eventPage, logLevel, logModule, logHours, logSearch], () => { void loader.load() })
const exportUrl = computed(() => api.eventExportUrl(logLevel.value, logModule.value, logSearch.value, logHours.value))

const scale = computed(() => (stats.value?.step_seconds ?? 3600) >= 21600 ? 'hour' as const : 'minute' as const)
const timelineChart = computed(() => (t: ChartTokens) => timeSeriesOption(t, {
  scale: scale.value, format: compact,
  series: [
    { name: '异常', color: t.status.critical, type: 'bar', stack: 'events', data: series(stats.value?.points, 'error') },
    { name: '操作记录', color: t.series[0], type: 'bar', stack: 'events', data: series(stats.value?.points, 'info') },
  ],
}))
const moduleChart = computed(() => (t: ChartTokens) => donutOption(t, {
  title: '事件总数', format: compact,
  data: (stats.value?.modules ?? []).slice(0, 5).map((row, index) => ({ name: MODULE_LABELS[row.module] ?? row.module, value: row.count, color: t.series[index] }))
    .concat((stats.value?.modules ?? []).length > 5 ? [{ name: '其他', value: (stats.value?.modules ?? []).slice(5).reduce((sum, row) => sum + row.count, 0), color: t.axis }] : []),
}))

async function openEvent(entry: api.AdminEvent) {
  selected.value = entry
  audit.value = null
  auditError.value = ''
  if (entry.level !== 'info') return
  try { audit.value = await api.getAuditDetail(entry.id) }
  catch (cause) { auditError.value = errorMessage(cause) }
}
</script>

<template>
  <AdminView title="日志与异常" description="持久化任务失败、管理审计事件与 API 进程内 5xx 的检索、分布与导出。" :loader="loader">
    <template #actions>
      <AdminSegmented v-model="logHours" :options="HOURS" label="时间范围" />
      <a class="admin-link-button" :href="exportUrl" download><Download class="h-4 w-4" />导出 CSV</a>
    </template>
    <div class="chart-grid">
      <ChartCard title="事件分布" :subtitle="`共 ${plain(stats?.total ?? 0)} 条 · 当前筛选`" :span="8"
                 :columns="[{ key: 'time', label: '时间', format: v => date(v) }, { key: 'error', label: '异常' }, { key: 'info', label: '操作记录' }]" :rows="stats?.points ?? []">
        <AdminChart :option="timelineChart" :height="220" label="事件时间分布柱状图" />
      </ChartCard>
      <ChartCard title="模块分布" :span="4">
        <AdminChart :option="moduleChart" :height="220" label="事件模块分布环形图" />
      </ChartCard>
    </div>

    <section class="table-panel">
      <div class="table-toolbar">
        <div class="controls">
          <select v-model="logLevel" aria-label="日志级别"><option value="all">全部级别</option><option value="error">仅异常</option><option value="info">操作记录</option></select>
          <select v-model="logModule" aria-label="日志模块"><option value="all">全部模块</option><option v-for="name in MODULES" :key="name" :value="name">{{ MODULE_LABELS[name] }}</option></select>
          <form class="search-field" role="search" @submit.prevent="logSearch = searchDraft.trim()"><Search class="h-4 w-4" aria-hidden="true" /><Input v-model="searchDraft" aria-label="搜索日志" placeholder="搜索类型、消息或 ID，回车确认" class="search" /></form>
        </div>
      </div>
      <AdminTable table-class="log-table">
        <thead><tr><th>时间</th><th>级别</th><th>模块</th><th>类型</th><th>摘要</th></tr></thead>
        <tbody>
          <tr v-for="entry in events" :key="entry.id" class="is-clickable" tabindex="0" :aria-selected="selected?.id === entry.id" @click="openEvent(entry)" @keydown.enter="openEvent(entry)">
            <td :title="date(entry.time)">{{ date(entry.time) }}<small>{{ relative(entry.time) }}</small></td>
            <td><StatusPill :label="entry.level === 'error' ? '异常' : '记录'" :tone="entry.level === 'error' ? 'negative' : 'neutral'" /></td>
            <td>{{ MODULE_LABELS[entry.module] ?? entry.module }}</td>
            <td class="mono">{{ entry.type }}</td>
            <td class="clip" :title="entry.message">{{ entry.message }}</td>
          </tr>
        </tbody>
      </AdminTable>
      <AdminEmptyState v-if="!events.length" title="当前范围内没有记录" description="尝试扩大时间范围或调整筛选条件。" />
      <Pager :page="eventPage" :page-count="Math.max(1, Math.ceil((pagination?.total ?? 0) / PAGE_SIZE))" :total="pagination?.total ?? 0" :page-size="PAGE_SIZE" unit="条" @update:page="eventPage = $event" />
    </section>

    <AdminDrawer v-if="selected" :title="selected.level === 'error' ? '异常详情' : '审计记录'" @close="selected = null">
      <div class="drawer-stack">
        <dl class="detail-grid">
          <div><dt>时间</dt><dd>{{ date(selected.time) }}</dd></div>
          <div><dt>模块</dt><dd>{{ MODULE_LABELS[selected.module] ?? selected.module }}</dd></div>
          <div><dt>类型</dt><dd class="mono">{{ selected.type }}</dd></div>
          <div><dt>ID</dt><dd class="mono">{{ selected.id }}</dd></div>
        </dl>
        <h3 class="drawer-heading">消息</h3>
        <pre class="code-block">{{ selected.message }}</pre>
        <template v-if="audit">
          <dl class="detail-grid">
            <div><dt>操作者</dt><dd>{{ audit.actor?.username ?? '系统' }}</dd></div>
            <div><dt>对象</dt><dd>{{ audit.target_type }} <span class="mono">{{ audit.target_id }}</span></dd></div>
          </dl>
          <h3 class="drawer-heading">附加数据</h3>
          <pre class="code-block">{{ JSON.stringify(audit.metadata, null, 2) }}</pre>
        </template>
        <p v-if="auditError" class="admin-error">{{ auditError }}</p>
        <RouterLink v-if="selected.level === 'error' && selected.module !== 'api'" :to="{ path: '/admin', query: { tab: 'tasks', search: selected.id } }">在任务与队列中查看此任务</RouterLink>
      </div>
    </AdminDrawer>
  </AdminView>
</template>
