<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RefreshCw } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { getQuota, listQuotaTransactions, type QuotaBalance, type QuotaTransaction } from '@/api/quota'
import { getProjectSummary, listProjects } from '@/api/project'
import { listProjectFiles } from '@/api/projectFiles'

const balance = ref<QuotaBalance | null>(null)
const transactions = ref<QuotaTransaction[]>([])
const storageBytes = ref<number | null>(null)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')

const dailyUsage = computed(() => {
  const today = new Date()
  const days = Array.from({ length: 7 }, (_, offset) => {
    const date = new Date(today)
    date.setDate(today.getDate() - (6 - offset))
    const key = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`
    const amount = transactions.value.filter((row) => ['consume', 'settle'].includes(row.kind) && (() => {
      const created = new Date(row.created_at)
      return `${created.getFullYear()}-${created.getMonth()}-${created.getDate()}` === key
    })()).reduce((sum, row) => sum + row.amount, 0)
    return { label: date.toLocaleDateString(undefined, { month: 'numeric', day: 'numeric' }), amount }
  })
  const max = Math.max(1, ...days.map((day) => day.amount))
  return days.map((day) => ({ ...day, height: day.amount ? Math.max(6, day.amount / max * 100) : 0 }))
})
const hasUsage = computed(() => transactions.value.some((row) => ['consume', 'settle'].includes(row.kind)))

function formatBytes(value: number | null) {
  if (value === null) return '未采集'
  if (value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  return `${(value / (1024 ** index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`
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
  <div class="usage-page">
    <header class="page-header usage-header">
      <div><p class="eyebrow">YOUR USAGE</p><h1 class="page-title">使用量</h1><p class="page-description">查看当前账户额度和真实存储用量。</p></div>
      <Button variant="outline" :disabled="refreshing" @click="load"><RefreshCw class="h-4 w-4" />刷新</Button>
    </header>

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
      <div class="section-heading"><div><h2>额度明细</h2><span class="muted">最近 {{ transactions.length }} 条记录</span></div></div>
      <Card v-if="loading" class="usage-empty">正在读取…</Card>
      <Card v-else-if="transactions.length" class="usage-table-card"><div class="usage-table"><table><thead><tr><th>时间</th><th>类型</th><th>说明</th><th>额度</th><th>可用余额</th></tr></thead>
        <tbody><tr v-for="row in transactions" :key="row.id"><td>{{ formatDate(row.created_at) }}</td><td>{{ row.resource_type ? `${row.operation_type || '模型调用'}（${row.resource_type}）` : transactionLabel(row.kind) }}</td><td>{{ row.resource_type === 'LLM' ? '模型输出' : row.resource_type === 'TTS' ? '合成输入' : row.note || row.task_id || '—' }}</td><td>{{ ['consume', 'settle'].includes(row.kind) ? `-${row.char_count ?? row.amount}` : `${row.amount > 0 ? '+' : ''}${row.amount}` }}</td><td>{{ row.available_after ?? '—' }}</td></tr></tbody></table></div></Card>
      <Card v-else class="usage-empty">暂无额度明细。开始处理任务后，记录会显示在这里。</Card>
    </section>
  </div>
</template>

<style scoped>
.usage-page{display:grid;gap:22px;max-width:1320px;margin:0 auto;padding-bottom:30px}.usage-header{display:flex;align-items:flex-end;justify-content:space-between;gap:14px}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.14em;color:hsl(var(--primary))}.usage-header h1{margin-top:5px}.usage-header .page-description{margin-top:4px}.usage-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.usage-card{display:grid;gap:5px;padding:14px}.usage-card>span,.usage-card small,.usage-card--plan>div{color:hsl(var(--muted-foreground));font-size:11px}.usage-card strong{font-size:21px;font-weight:750;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.usage-card--plan>div{display:flex;justify-content:space-between;align-items:center;gap:6px}.usage-card--plan strong{font-size:18px}.usage-note{display:flex;align-items:flex-start;gap:12px;padding:12px 14px}.usage-note strong{flex:none;font-size:11px}.usage-note p{color:hsl(var(--muted-foreground));font-size:11px;line-height:1.5}.usage-section{display:grid;gap:10px}.section-heading{display:flex;justify-content:space-between;align-items:center;gap:10px}.section-heading h2{font-size:15px;font-weight:750}.muted{color:hsl(var(--muted-foreground));font-size:11px}.usage-charts .section-heading>div{display:flex;align-items:baseline;gap:9px}.usage-chart{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:12px;min-height:180px;padding:16px 20px}.chart-day{display:grid;grid-template-rows:16px 1fr 16px;justify-items:center;gap:7px;min-width:0}.chart-day>strong{font-size:10px;font-variant-numeric:tabular-nums}.chart-track{display:flex;align-items:flex-end;justify-content:center;width:100%;max-width:52px;border-radius:7px;background:hsl(var(--muted)/.55)}.chart-track span{display:block;width:100%;min-height:0;border-radius:7px;background:hsl(var(--primary));transition:height .2s}.chart-day small{color:hsl(var(--muted-foreground));font-size:10px}.usage-empty{padding:22px;text-align:center;color:hsl(var(--muted-foreground));font-size:12px}.usage-table-card{overflow:hidden}.usage-table{overflow-x:auto}table{width:100%;min-width:620px;border-collapse:collapse;font-size:11px}th,td{padding:10px 12px;border-bottom:1px solid hsl(var(--border));text-align:left}th{color:hsl(var(--muted-foreground));font-weight:650;white-space:nowrap}.usage-alert{display:flex;align-items:center;justify-content:space-between;gap:12px;border:1px solid hsl(var(--destructive)/.25);border-radius:9px;padding:10px 12px;color:hsl(var(--destructive));font-size:12px}@media(max-width:1000px){.usage-grid{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:650px){.usage-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.usage-note{flex-direction:column}.usage-charts .section-heading>div{align-items:flex-start;flex-direction:column;gap:2px}.usage-chart{gap:7px;padding:13px 10px}}
</style>
