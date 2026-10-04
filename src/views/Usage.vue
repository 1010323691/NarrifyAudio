<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, RefreshCw, Search, WalletCards } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import { getQuota, listQuotaTransactions, type QuotaBalance, type QuotaTransaction } from '@/api/quota'
import { getProjectSummary, listProjects } from '@/api/project'
import { listProjectFiles } from '@/api/projectFiles'
import { formatBytes as formatBytesBase, type BytesFormat } from '@/utils/format'

const balance = ref<QuotaBalance | null>(null)
const transactions = ref<QuotaTransaction[]>([])
const storageBytes = ref<number | null>(null)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const search = ref('')
const transactionFilter = ref('all')
const page = ref(1)
const pageSize = 20

const dailyUsage = computed(() => {
  const today = new Date()
  const days = Array.from({ length: 7 }, (_, offset) => {
    const date = new Date(today)
    date.setDate(today.getDate() - (6 - offset))
    const key = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`
    const amount = transactions.value.filter((row) => ['consume', 'settle'].includes(row.kind) && (() => {
      const created = new Date(row.created_at)
      return `${created.getFullYear()}-${created.getMonth()}-${created.getDate()}` === key
    })()).reduce((sum, row) => sum + Math.abs(row.amount), 0)
    return { label: date.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' }), amount }
  })
  const max = Math.max(1, ...days.map((day) => day.amount))
  return days.map((day) => ({ ...day, height: day.amount ? Math.max(6, day.amount / max * 100) : 0 }))
})
const hasUsage = computed(() => transactions.value.some((row) => ['consume', 'settle'].includes(row.kind)))
const filteredTransactions = computed(() => transactions.value.filter((row) => {
  const matchesType = transactionFilter.value === 'all'
    || (transactionFilter.value === 'consume' ? ['consume', 'settle'].includes(row.kind) : row.kind === transactionFilter.value)
  const text = `${row.note || ''} ${row.task_id || ''} ${row.operation_type || ''} ${row.resource_type || ''}`.toLocaleLowerCase()
  return matchesType && (!search.value.trim() || text.includes(search.value.trim().toLocaleLowerCase()))
}))
const pagedTransactions = computed(() => filteredTransactions.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const pageCount = computed(() => Math.max(1, Math.ceil(filteredTransactions.value.length / pageSize)))
watch([search, transactionFilter], () => { page.value = 1 })

// 本页大小口径：未采集显示占位文案，零/负值统一 0 B，KB 及以上 1 位小数
const USAGE_BYTES: BytesFormat = { emptyText: '未采集', lowRange: 'clamp', decimals: 'always-one' }
function formatBytes(value: number | null) {
  return formatBytesBase(value, USAGE_BYTES)
}

function transactionLabel(kind: string) {
  return ({ consume: '额度消费', reserve: '额度预留', settle: '任务消费', release: '额度退回', admin_adjust: '额度调整' } as Record<string, string>)[kind] || kind
}

function formatDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
}

async function load() {
  loading.value = !balance.value
  refreshing.value = true
  error.value = ''
  const [balanceResult, transactionResult, projectsResult] = await Promise.allSettled([
    getQuota(), listQuotaTransactions(), listProjects(),
  ])
  if (balanceResult.status === 'fulfilled') balance.value = balanceResult.value
  else error.value = balanceResult.reason?.message || '额度暂时无法读取。'
  if (transactionResult.status === 'fulfilled') transactions.value = transactionResult.value
  else if (!error.value) error.value = transactionResult.reason?.message || '使用记录暂时无法读取。'
  if (projectsResult.status === 'fulfilled') {
    const usage = await Promise.all(projectsResult.value.map(async (project) => {
      try { return (await getProjectSummary(project.id)).size_bytes }
      catch {
        try { return (await listProjectFiles(project.id)).reduce((sum, file) => sum + file.size_bytes, 0) }
        catch { return null }
      }
    }))
    storageBytes.value = usage.every((value) => value !== null) ? usage.reduce<number>((sum, value) => sum + (value || 0), 0) : null
  } else storageBytes.value = null
  refreshing.value = false
  loading.value = false
}

onMounted(load)
</script>

<template>
  <div class="usage-page viewport-page">
    <WorkbenchContextBar>
      <template #icon><WalletCards /></template>
      <template #title>使用量</template>
      <template #description>查看账户额度、项目存储和实际处理记录。</template>
      <template #metrics>
        <div class="workbench-context-metric"><strong>{{ loading ? '…' : balance?.available_units ?? '—' }}</strong>可用额度</div>
        <div class="workbench-context-metric"><strong>{{ loading ? '…' : balance?.reserved_units ?? '—' }}</strong>预留额度</div>
      </template>
      <template #actions><Button variant="outline" :disabled="refreshing" @click="load"><RefreshCw class="h-4 w-4" :class="refreshing ? 'animate-spin' : ''" />{{ refreshing ? '刷新中' : '刷新' }}</Button></template>
    </WorkbenchContextBar>

    <div class="page-region" role="region" aria-label="页面工作区" tabindex="0">
      <div v-if="error" class="usage-alert" role="alert">{{ error }} <Button variant="outline" size="sm" @click="load">重试</Button></div>

      <section class="usage-grid">
        <Card class="usage-card usage-card--plan"><div><span>当前套餐</span><StatusPill label="未配置套餐" tone="neutral" /></div><strong>—</strong><small>套餐信息尚未由平台提供。</small></Card>
        <Card class="usage-card"><span>可用额度</span><strong>{{ loading ? '…' : balance?.available_units ?? '…' }}</strong><small>LLM 与 TTS 实际调用计量</small></Card>
        <Card class="usage-card"><span>预留额度</span><strong>{{ loading ? '…' : balance?.reserved_units ?? '…' }}</strong><small>正在运行或排队的任务</small></Card>
        <Card class="usage-card"><span>累计消耗</span><strong>{{ loading ? '…' : balance?.consumed_units ?? '…' }}</strong><small>已记录的 LLM 与 TTS 实际处理量</small></Card>
        <Card class="usage-card"><span>项目存储</span><strong>{{ loading ? '…' : formatBytes(storageBytes) }}</strong><small>只统计本人项目</small></Card>
      </section>

      <Card class="usage-note"><strong>用量口径</strong><p>LLM 按最终有效输出量扣减；TTS 按实际输入内容预留，并对成功合成的部分扣减、退回未执行部分。Prompt、Token、请求次数和音频时长不计费。</p></Card>

      <section class="usage-section usage-charts">
        <div class="section-heading"><div><h2>近 7 天额度消耗</h2><span class="muted">来源：已完成任务账本</span></div></div>
        <Card v-if="loading" class="usage-empty">正在读取使用记录…</Card>
        <Card v-else-if="hasUsage" class="usage-chart">
          <div v-for="day in dailyUsage" :key="day.label" class="chart-day">
            <strong>{{ day.amount || '' }}</strong><div class="chart-track"><span :style="{ height: `${day.height}%` }" /></div><small>{{ day.label }}</small>
          </div>
        </Card>
        <Card v-else class="usage-empty">暂时没有已完成任务的额度消耗记录。</Card>
      </section>

      <section class="usage-section">
        <div class="section-heading usage-transaction-heading">
          <div><h2>额度明细</h2><span class="muted">最近 {{ transactions.length }} 条记录</span></div>
          <div class="usage-filters">
            <label class="usage-search"><Search class="h-4 w-4" /><input v-model="search" aria-label="搜索额度明细" placeholder="搜索说明、任务编号或模型" /></label>
            <select v-model="transactionFilter" aria-label="筛选额度明细类型"><option value="all">全部类型</option><option value="consume">额度消耗</option><option value="reserve">额度预留</option><option value="release">额度退回</option><option value="admin_adjust">额度调整</option></select>
          </div>
        </div>
        <Card v-if="loading" class="usage-empty">正在读取…</Card>
        <Card v-else-if="pagedTransactions.length" class="usage-table-card"><div class="usage-table"><table class="workbench-table"><thead><tr><th>时间</th><th>类型</th><th>说明</th><th>额度</th><th>可用余额</th></tr></thead>
          <tbody><tr v-for="row in pagedTransactions" :key="row.id"><td>{{ formatDate(row.created_at) }}</td><td>{{ row.resource_type ? `${row.operation_type || '模型调用'}（${row.resource_type}）` : transactionLabel(row.kind) }}</td><td>{{ row.resource_type === 'LLM' ? '模型输出' : row.resource_type === 'TTS' ? '合成输入' : row.note || row.task_id || '—' }}</td><td>{{ ['consume', 'settle'].includes(row.kind) ? `-${row.char_count ?? Math.abs(row.amount)}` : `${row.amount > 0 ? '+' : ''}${row.amount}` }}</td><td>{{ row.available_after ?? '—' }}</td></tr></tbody></table></div>
          <div class="usage-pagination"><span>第 {{ (page - 1) * pageSize + 1 }}–{{ Math.min(page * pageSize, filteredTransactions.length) }} 条，共 {{ filteredTransactions.length }} 条</span><div><Button size="sm" variant="outline" :disabled="page <= 1" aria-label="上一页" @click="page--"><ChevronLeft class="h-4 w-4" /></Button><span>{{ page }} / {{ pageCount }}</span><Button size="sm" variant="outline" :disabled="page >= pageCount" aria-label="下一页" @click="page++"><ChevronRight class="h-4 w-4" /></Button></div></div>
        </Card>
        <Card v-else class="usage-empty">{{ transactions.length ? '没有符合筛选条件的记录。' : '暂无额度明细。开始处理任务后，记录会显示在这里。' }}</Card>
      </section>
    </div>
  </div>
</template>

<style scoped>
.usage-page{gap:10px}.usage-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.usage-card{display:grid;gap:5px;padding:14px}.usage-card>span,.usage-card small,.usage-card--plan>div{color:hsl(var(--muted-foreground));font-size:11px}.usage-card strong{font-size:21px;font-weight:750;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.usage-card--plan>div{display:flex;justify-content:space-between;align-items:center;gap:6px}.usage-card--plan strong{font-size:18px}.usage-note{display:flex;align-items:flex-start;gap:12px;padding:12px 14px}.usage-note strong{flex:none;font-size:11px}.usage-note p{color:hsl(var(--muted-foreground));font-size:11px;line-height:1.5}.usage-section{display:grid;gap:10px}.section-heading{display:flex;justify-content:space-between;align-items:center;gap:10px}.section-heading h2{font-size:14px;font-weight:750}.muted{color:hsl(var(--muted-foreground));font-size:11px}.usage-charts .section-heading>div{display:flex;align-items:baseline;gap:9px}.usage-chart{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:12px;min-height:150px;padding:14px 20px}.chart-day{display:grid;grid-template-rows:16px 1fr 16px;justify-items:center;gap:7px;min-width:0}.chart-day>strong{font-size:10px;font-variant-numeric:tabular-nums}.chart-track{display:flex;align-items:flex-end;justify-content:center;width:100%;max-width:52px;border-radius:7px;background:hsl(var(--muted)/.55)}.chart-track span{display:block;width:100%;min-height:0;border-radius:7px;background:linear-gradient(180deg,hsl(199 89% 58%),hsl(var(--primary)));transition:height .2s}.chart-day small{color:hsl(var(--muted-foreground));font-size:10px}.usage-empty{padding:22px;text-align:center;color:hsl(var(--muted-foreground));font-size:12px}.usage-table-card{overflow:hidden}.usage-table{overflow-x:auto}table{width:100%;min-width:620px;border-collapse:collapse;font-size:11px}.usage-alert{display:flex;align-items:center;justify-content:space-between;gap:12px;border:1px solid hsl(var(--destructive)/.25);border-radius:9px;padding:10px 12px;color:hsl(var(--destructive));font-size:12px}.usage-transaction-heading>div:first-child{display:flex;align-items:baseline;gap:9px}.usage-filters{display:flex;align-items:center;gap:8px}.usage-search{display:flex;align-items:center;gap:7px;width:min(280px,35vw);height:34px;padding:0 10px;border:1px solid hsl(var(--input));border-radius:9px;color:hsl(var(--muted-foreground));background:hsl(var(--card))}.usage-search input{min-width:0;width:100%;border:0;outline:0;background:transparent;color:hsl(var(--foreground));font-size:12px}.usage-filters select{height:34px;padding:0 10px;border:1px solid hsl(var(--input));border-radius:9px;background:hsl(var(--card));font-size:12px}.usage-pagination{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border-top:1px solid hsl(var(--border));color:hsl(var(--muted-foreground));font-size:11px}.usage-pagination>div{display:flex;align-items:center;gap:8px}.usage-pagination button{width:30px;height:30px;padding:0}@media(max-width:1000px){.usage-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:650px){.usage-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.usage-note{flex-direction:column}.usage-charts .section-heading>div{align-items:flex-start;flex-direction:column;gap:2px}.usage-chart{gap:7px;padding:13px 10px}.usage-transaction-heading{align-items:flex-start;flex-direction:column}.usage-filters{width:100%}.usage-search{width:auto;flex:1}}
</style>
