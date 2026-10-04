<script setup lang="ts">
import { computed, onActivated, onDeactivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import AdminStatCard from '@/components/admin/AdminStatCard.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import AdminPageHeader from '@/components/admin/AdminPageHeader.vue'
import AdminDrawer from '@/components/admin/AdminDrawer.vue'
import AdminEmptyState from '@/components/admin/AdminEmptyState.vue'
import AdminLoadingState from '@/components/admin/AdminLoadingState.vue'
import { useRoute } from 'vue-router'
import { ArrowUpRight, RefreshCw, RotateCcw, Trash2 } from 'lucide-vue-next'
import { useClientDisplayStore } from '@/stores/clientDisplay'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Input from '@/components/ui/Input.vue'
import Switch from '@/components/ui/Switch.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import * as api from '@/api/admin'
import { modulePrefixes } from '@/utils/taskTypes'
import { formatBytes, type BytesFormat } from '@/utils/format'
import AdminSettings from '@/components/settings/AdminSettings.vue'
import GpuScheduler from '@/components/settings/GpuScheduler.vue'

type Tab = 'overview' | 'performance' | 'users' | 'resources' | 'settings' | 'tasks' | 'logs'
type SettingsSection = 'text' | 'models' | 'audio' | 'general' | 'storage' | 'runtime'
const validTabs = new Set<Tab>(['overview', 'performance', 'users', 'resources', 'settings', 'tasks', 'logs'])
const clientDisplay = useClientDisplayStore()
const route = useRoute()
const tab = computed<Tab>(() => validTabs.has(route.query.tab as Tab) ? route.query.tab as Tab : 'overview')
const { push: toast } = useToast()
const pageMeta: Record<Tab, [string, string]> = {
  overview: ['系统总览', '掌握平台运行状态、任务负载与需要关注的异常。'],
  performance: ['性能监控', '查看主机资源、GPU 服务和 Worker 实时快照。'],
  users: ['用户管理', '管理账户权限、启用状态与制作额度。'],
  resources: ['资源与存储', '查看工作空间占用与公共资源，安全清理过期缓存。'],
  settings: ['系统配置', '统一管理平台制作参数、存储与运行设置。'],
  tasks: ['任务与队列', '跟踪平台制作任务，查看详情并处理失败任务。'],
  logs: ['日志与异常', '检索持久化任务失败、审计事件与 API 异常。'],
}
const loadedTabs = ref(new Set<Tab>())
let loadEpoch = 0
let active = true
const lastUpdated = ref<Partial<Record<Tab, string>>>({})

const overview = ref<api.AdminOverview | null>(null)
const performance = ref<api.AdminPerformance | null>(null)
const taskMetrics = ref<api.TaskMetrics | null>(null)
const resources = ref<api.AdminResources | null>(null)
const users = ref<api.AdminUser[]>([])
const tasks = ref<api.AdminTask[]>([])
const events = ref<api.AdminEvent[]>([])
const storage = ref<api.StorageSettings | null>(null)
const quota = ref<api.QuotaSettings | null>(null)
const registration = ref<api.RegistrationSettings | null>(null)
const runtime = ref<api.RuntimeSettings | null>(null)
const settingsSection = ref<SettingsSection>('text')
const rootDraft = ref('')
const quotaDraft = ref('0')
const userSearch = ref('')
const userRole = ref('all')
const userState = ref('all')
const userSort = ref('default')
const userPage = ref(1)
const selectedUser = ref<api.AdminUser | null>(null)
const quotaAmount = ref('')
const taskSearch = ref('')
const taskStatus = ref('all')
const selectedTask = ref<api.AdminTask | null>(null)
const logSearch = ref('')
const logLevel = ref('all')
const logHours = ref(24)
const logModule = ref('all')
const loading = ref(false)
const error = ref('')
const actionBusy = ref(false)
const savingToggle = ref<'logs' | 'registration' | null>(null)
const savingQuota = ref(false)
async function submitQuota() {
  if (actionBusy.value) return
  savingQuota.value = true
  try { await runAction(saveQuota) }
  finally { savingQuota.value = false }
}
async function saveToggle(kind: 'logs' | 'registration') {
  if (actionBusy.value) return
  savingToggle.value = kind
  try { await runAction(kind === 'logs' ? toggleClientLogs : toggleRegistration) }
  finally { savingToggle.value = null }
}
async function runAction(action: () => Promise<void>) {
  if (actionBusy.value) return
  const actionTab = tab.value
  const resumeLoad = loading.value
  actionBusy.value = true
  // A read begun before this mutation must not overwrite its successful response.
  loadEpoch++
  loading.value = false
  error.value = ''
  try {
    await action()
    if (error.value) toast({ title: '操作未完成', description: error.value, variant: 'destructive' })
  } finally {
    actionBusy.value = false
    if (active && (resumeLoad || tab.value !== actionTab || !loadedTabs.value.has(tab.value))) await load()
  }
}
let timer: ReturnType<typeof setInterval> | null = null

const matchingUsers = computed(() => {
  const query = userSearch.value.trim().toLowerCase()
  const rows = users.value.filter(row =>
    `${row.username} ${row.display_name} ${row.email}`.toLowerCase().includes(query)
    && (userRole.value === 'all' || row.role === userRole.value)
    && (userState.value === 'all' || row.is_active === (userState.value === 'active')),
  )
  if (userSort.value === 'name') rows.sort((a, b) => a.username.localeCompare(b.username, 'zh-CN'))
  if (userSort.value === 'storage') rows.sort((a, b) => (b.storage_bytes ?? 0) - (a.storage_bytes ?? 0))
  if (userSort.value === 'recent') rows.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
  return rows
})
const userFiltered = computed(() => !!userSearch.value || userRole.value !== 'all' || userState.value !== 'all')
function clearUserFilters() { userSearch.value = ''; userRole.value = 'all'; userState.value = 'all' }
const userPages = computed(() => Math.max(1, Math.ceil(matchingUsers.value.length / 20)))
const shownUsers = computed(() => matchingUsers.value.slice((userPage.value - 1) * 20, userPage.value * 20))

function aggregateTypes(prefixes: string[]) {
  const rows = performance.value?.tasks.by_type ?? []
  return rows.filter(row => prefixes.some(prefix => row.task_type.startsWith(prefix)))
    .reduce((total, row) => {
      for (const [status, count] of Object.entries(row.statuses)) total[status] = (total[status] ?? 0) + count
      return total
    }, {} as Record<string, number>)
}
// 服务归类（模块 → 前缀，单源见 utils/taskTypes.ts）：tts.merge 按 ffmpeg 口径计入 ffmpeg
const serviceTaskMetrics = computed(() => ({
  llm: aggregateTypes(modulePrefixes('script', 'music')),
  tts: aggregateTypes(modulePrefixes('tts', 'voices', 'preview')),
  ffmpeg: aggregateTypes(modulePrefixes('audio', 'merge')),
  bgm: aggregateTypes(modulePrefixes('bgm')),
}))
const taskStatusSummary = computed(() => taskMetrics.value?.status_counts ?? {})
const resourceCategories = computed(() => resources.value?.project_storage?.categories ?? [])
const cleanupCandidates = computed(() => resources.value?.project_storage?.cleanup_candidates)

watch([userSearch, userRole, userState, userSort], () => { userPage.value = 1 })
watch(userPages, pages => { userPage.value = Math.min(userPage.value, pages) })
function date(value?: string | null) { return value ? new Date(value).toLocaleString('zh-CN') : '—' }
// 管理端大小口径：缺失显示「未采集」，<1024 原样（含负值），KB+ 一律 1 位小数
const ADMIN_BYTES: BytesFormat = { emptyText: '未采集', lowRange: 'raw', decimals: 'always-one' }
function bytes(value?: number | null) {
  return formatBytes(value, ADMIN_BYTES)
}
function elapsed(start?: string | null, end?: string | null) {
  if (!start) return '—'
  const seconds = Math.max(0, Math.floor(((end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime()) / 1000))
  return seconds < 60 ? `${seconds} 秒` : seconds < 3600 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`
}
function metricCount(statuses: Record<string, number>, ...keys: string[]) {
  return keys.reduce((total, key) => total + (statuses[key] ?? 0), 0)
}
function tone(status: string): 'positive' | 'warning' | 'negative' | 'neutral' {
  if (['healthy', 'succeeded', 'completed', 'idle'].includes(status)) return 'positive'
  if (['warning', 'queued', 'pending', 'retrying', 'processing', 'running', 'cancelling'].includes(status)) return 'warning'
  if (['error', 'failed', 'timeout', 'offline'].includes(status)) return 'negative'
  return 'neutral'
}
function statusLabel(status: string) {
  return ({ healthy: '正常', warning: '警告', error: '异常', unknown: '未采集', pending: '排队中', queued: '排队中',
    cancelling: '取消中', running: '运行中', processing: '运行中', retrying: '重试中', succeeded: '已完成', completed: '已完成',
    failed: '失败', timeout: '超时', cancelled: '已取消', idle: '空闲', offline: '离线' } as Record<string, string>)[status] ?? status
}
async function load() {
  if (actionBusy.value) return
  await loadData()
}
// Mutation handlers may refresh their result while keeping external reads blocked.
async function loadData() {
  const currentTab = tab.value
  const ticket = ++loadEpoch
  loading.value = true
  error.value = ''
  try {
    if (currentTab === 'overview') {
      const [current, metrics] = await Promise.all([api.getOverview(), api.getTaskMetrics()])
      if (ticket !== loadEpoch) return
      overview.value = current
      taskMetrics.value = metrics
    }
    else if (currentTab === 'performance') {
      const result = await api.getPerformance()
      if (ticket !== loadEpoch) return
      performance.value = result
    }
    else if (currentTab === 'users') {
      const result = await api.listUsers()
      if (ticket !== loadEpoch) return
      users.value = result
    }
    else if (currentTab === 'resources') {
      const result = await api.getResources()
      if (ticket !== loadEpoch) return
      resources.value = result
    }
    else if (currentTab === 'settings') {
      await clientDisplay.load()
      const [s, q, r] = await Promise.all([api.getStorageSettings(), api.getQuotaSettings(), api.getRegistrationSettings()])
      if (ticket !== loadEpoch) return
      storage.value = s
      quota.value = q
      registration.value = r
      rootDraft.value = s.root_path
      quotaDraft.value = String(q.initial_units)
      const limits = await api.getRuntimeSettings().catch(() => null)
      if (ticket !== loadEpoch) return
      runtime.value = limits
    } else if (currentTab === 'tasks') {
      const [rows, metrics] = await Promise.all([api.listTasks(taskStatus.value, taskSearch.value), api.getTaskMetrics()])
      if (ticket !== loadEpoch) return
      tasks.value = rows
      taskMetrics.value = metrics
      if (selectedTask.value) selectedTask.value = rows.find(row => row.id === selectedTask.value?.id) ?? selectedTask.value
    } else {
      const result = await api.getEvents(logLevel.value, logModule.value, logSearch.value, logHours.value)
      if (ticket !== loadEpoch) return
      events.value = result
    }
    if (ticket === loadEpoch) { loadedTabs.value.add(currentTab); lastUpdated.value[currentTab] = new Date().toLocaleTimeString('zh-CN') }
  } catch (cause: any) {
    if (ticket === loadEpoch) error.value = cause?.message || String(cause)
  } finally {
    if (ticket === loadEpoch) loading.value = false
  }
}
watch(tab, () => { selectedUser.value = null; selectedTask.value = null; void load() })
onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (active && !loading.value && !actionBusy.value && document.visibilityState === 'visible' && ['overview', 'performance', 'tasks'].includes(tab.value)) void load()
  }, 15000)
})
onActivated(() => { if (!active) { active = true; void load() } })
onDeactivated(() => { active = false; loadEpoch++; loading.value = false })
onBeforeUnmount(() => { loadEpoch++; if (timer) clearInterval(timer) })

async function toggleClientLogs() {
  try {
    if (await clientDisplay.save(!clientDisplay.logsEnabled)) toast({ title: '客户端日志显示设置已保存', variant: 'success' })
  } catch (cause: any) { error.value = cause?.message || String(cause) }
}

async function toggleRegistration() {
  if (!registration.value) return
  try { registration.value = await api.updateRegistrationSettings(!registration.value.enabled); toast({ title: '注册设置已保存', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function saveQuota() {
  const value = Number(quotaDraft.value)
  if (!Number.isInteger(value) || value < 0) { error.value = '初始额度必须是非负整数'; return }
  try { quota.value = await api.updateQuotaSettings(value); toast({ title: '初始额度已保存', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function saveRoot() {
  if (!await showConfirm('修改存储根目录会迁移已登记工作空间文件。确认继续？', { title: '修改存储根目录', destructive: true })) return
  try { storage.value = await api.updateStorageRoot(rootDraft.value); toast({ title: '存储根目录已保存', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
function lastAdmin(user: api.AdminUser) {
  return user.role === 'admin' && user.is_active && users.value.filter(row => row.role === 'admin' && row.is_active).length <= 1
}
async function changeUser(user: api.AdminUser, patch: { is_active?: boolean; role?: 'user' | 'admin' }) {
  if (!await showConfirm(`确认更改 ${user.username} 的${patch.role ? '角色' : '状态'}？`, { title: '确认修改用户' })) return
  try {
    const result = await api.updateUser(user.id, patch)
    user.role = result.role
    user.is_active = result.is_active
    toast({ title: '用户已更新', variant: 'success' })
  } catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function adjustQuota() {
  const user = selectedUser.value
  const amount = Number(quotaAmount.value)
  if (!user || !Number.isInteger(amount) || amount === 0) { error.value = '请输入非零整数额度'; return }
  if (!await showConfirm(`确认给 ${user.username} 调整 ${amount > 0 ? '+' : ''}${amount} 单位额度？`, { title: '调整用户额度', destructive: amount < 0 })) return
  try {
    await api.adjustQuota(user.id, amount, `admin-${user.id}-${Date.now()}`, '管理员后台调整')
    quotaAmount.value = ''
    users.value = await api.listUsers()
    selectedUser.value = users.value.find(row => row.id === user.id) ?? null
    toast({ title: '额度已调整', variant: 'success' })
  } catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function cancelTask(task: api.AdminTask) {
  if (!await showConfirm(`确认取消任务 ${task.id}？`, { title: '取消任务', destructive: true })) return
  try { await api.cancelTask(task.id); selectedTask.value = null; await loadData(); toast({ title: '取消请求已提交', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function retryTask(task: api.AdminTask) {
  if (!await showConfirm(`将任务 ${task.id} 重新放入队列。确认重试？`, { title: '重新排入任务' })) return
  try { await api.retryTask(task.id); selectedTask.value = null; await loadData(); toast({ title: '任务已重新排队', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function cleanupTemp() {
  const candidates = cleanupCandidates.value
  if (!candidates?.count) return
  if (!await showConfirm(`仅清理不活跃工作空间内超过 ${candidates.older_than_days} 天的临时缓存文件，预计 ${candidates.count} 个、${bytes(candidates.size_bytes)}。此操作不可恢复，继续？`, { title: '清理临时缓存', destructive: true })) return
  try {
    const result = await api.cleanupStaleTemp()
    await loadData()
    toast({ title: `已清理 ${result.deleted_count} 个文件 · ${bytes(result.deleted_bytes)}`, variant: 'success' })
  } catch (cause: any) { error.value = cause?.message || String(cause) }
}
</script>

<template>
  <div class="admin-console">
    <AdminPageHeader :title="pageMeta[tab][0]" :description="pageMeta[tab][1]">
      <span v-if="lastUpdated[tab]" class="admin-updated">更新于 {{ lastUpdated[tab] }}</span>
      <Button variant="outline" size="sm" :disabled="loading || actionBusy" @click="load"><RefreshCw class="h-4 w-4" :class="{ 'animate-spin': loading }" />{{ loading ? '刷新中' : '刷新数据' }}</Button>
    </AdminPageHeader>
    <p v-if="error" class="admin-error" role="alert"><strong>无法完成请求</strong> · {{ error }} <button @click="load">重新尝试</button></p>
    <AdminLoadingState v-if="loading && !loadedTabs.has(tab)" />
    <div v-show="loadedTabs.has(tab)" :aria-busy="loading">

    <section v-if="tab === 'overview' && overview" class="admin-section">
      <div class="admin-title">
        <p>系统健康</p>
        <StatusPill :label="overview.services.some(item => item.status === 'error') ? '存在异常' : overview.services.some(item => item.status === 'warning') ? '需要关注' : '运行正常'" :tone="overview.services.some(item => item.status === 'error') ? 'negative' : overview.services.some(item => item.status === 'warning') ? 'warning' : 'positive'" />
      </div>
      <div class="metric-grid">
        <AdminStatCard label="运行中任务"><template #value>{{ overview.tasks.running }}</template></AdminStatCard>
        <AdminStatCard label="排队任务"><template #value>{{ overview.tasks.queued }}</template></AdminStatCard>
        <AdminStatCard label="失败任务" :value-class="metricCount(taskMetrics?.status_counts ?? {}, 'failed', 'timeout') ? 'text-danger' : ''"><template #value>{{ metricCount(taskMetrics?.status_counts ?? {}, 'failed', 'timeout') }}</template></AdminStatCard>
        <AdminStatCard label="今日 API 请求"><template #value>{{ overview.today.api_requests ?? '未采集' }}</template>{{ overview.today.api_requests_scope }}</AdminStatCard>
        <AdminStatCard label="活跃用户 · 15 分钟"><template #value>{{ overview.today.active_users }}</template></AdminStatCard>
        <AdminStatCard label="今日 TTS 字符" value-class="metric-optional"><template #value>{{ overview.today.tts_characters ?? '未采集' }}</template></AdminStatCard>
        <AdminStatCard label="今日 LLM Token" value-class="metric-optional"><template #value>{{ overview.today.llm_tokens ?? '未采集' }}</template></AdminStatCard>
        <AdminStatCard label="今日完成任务"><template #value>{{ overview.today.completed }}</template></AdminStatCard>
      </div>
      <div class="service-grid">
        <div v-for="service in overview.services" :key="service.key" class="service-row">
          <div><strong>{{ service.name }}</strong><StatusPill :label="statusLabel(service.status)" :tone="tone(service.status)" /></div>
          <p :title="service.detail">{{ service.detail }}</p>
        </div>
      </div>
      <div class="overview-grid">
        <Card>
          <CardHeader><CardTitle>系统资源</CardTitle></CardHeader>
          <CardContent class="kv-list">
            <p><span>CPU</span><strong>{{ overview.system.cpu_percent == null ? '未采集' : `${overview.system.cpu_percent}%` }}</strong></p>
            <p><span>RAM</span><strong>{{ bytes(overview.system.ram_used_bytes) }} / {{ bytes(overview.system.ram_total_bytes) }}</strong></p>
            <p><span>GPU</span><strong>{{ overview.gpu.length ? overview.gpu.map(item => `${item.name}: ${item.utilization_percent ?? '—'}% · VRAM ${item.memory_used_mb ?? '—'} / ${item.memory_total_mb ?? '—'} MB`).join('；') : '未采集' }}</strong></p>
            <p><span>Worker 槽位</span><strong>{{ overview.workers.active_slots }} / {{ overview.workers.total_slots }}</strong></p>
            <RouterLink to="/admin?tab=performance">服务与性能详情 <ArrowUpRight class="inline h-3.5 w-3.5" /></RouterLink>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>最近异常</CardTitle></CardHeader>
          <CardContent>
            <AdminTable v-if="overview.recent_errors.length" table-class="compact-table"><thead><tr><th>时间</th><th>模块</th><th>类型</th><th>摘要</th></tr></thead>
                <tbody><tr v-for="entry in overview.recent_errors" :key="entry.id"><td>{{ date(entry.time) }}</td><td><StatusPill :label="entry.module" /></td><td>{{ entry.type }}</td><td class="clip" :title="entry.message">{{ entry.message }}</td></tr></tbody>
              </AdminTable>
            <p v-else class="admin-muted">暂无近期异常记录</p>
            <RouterLink to="/admin?tab=logs">打开日志与异常 <ArrowUpRight class="inline h-3.5 w-3.5" /></RouterLink>
          </CardContent>
        </Card>
      </div>
    </section>

    <section v-if="tab === 'performance' && performance" class="admin-section">
      <div class="admin-title"><div><p>实时快照 · {{ date(performance.generated_at) }}</p></div></div>
      <div class="overview-grid">
        <Card><CardHeader><CardTitle>系统</CardTitle></CardHeader><CardContent class="kv-list">
          <p><span>CPU</span><strong>{{ performance.system.cpu_percent == null ? '未采集' : `${performance.system.cpu_percent}%` }}</strong></p>
          <p><span>RAM</span><strong>{{ bytes(performance.system.ram_used_bytes) }} / {{ bytes(performance.system.ram_total_bytes) }}</strong></p>
          <p><span>磁盘</span><strong>{{ bytes(performance.system.disk_used_bytes) }} / {{ bytes(performance.system.disk_total_bytes) }}</strong></p>
          <p><span>网络 / 磁盘 I/O</span><strong>未采集</strong></p>
          <p><span>运行时间</span><strong>{{ performance.system.uptime_seconds == null ? '未采集' : elapsed(new Date(Date.now() - performance.system.uptime_seconds * 1000).toISOString()) }}</strong></p>
        </CardContent></Card>
        <Card><CardHeader><CardTitle>GPU</CardTitle></CardHeader><CardContent>
          <template v-if="performance.gpu.length">
            <div v-for="gpu in performance.gpu" :key="gpu.index" class="gpu-block">
              <h3>{{ gpu.index }} · {{ gpu.name }}</h3>
              <div class="kv-list"><p><span>利用率</span><strong>{{ gpu.utilization_percent == null ? '未采集' : `${gpu.utilization_percent}%` }}</strong></p>
                <p><span>显存</span><strong>{{ gpu.memory_used_mb == null ? '未采集' : `${gpu.memory_used_mb} / ${gpu.memory_total_mb} MB` }}</strong></p>
                <p><span>温度 / 功耗</span><strong>{{ gpu.temperature_c == null ? '未采集' : `${gpu.temperature_c} °C` }} / {{ gpu.power_w == null ? '未采集' : `${gpu.power_w} W` }}</strong></p></div>
            </div>
          </template><p v-else class="admin-muted">当前 API 主机未检测到 GPU 指标</p>
        </CardContent></Card>
      </div>
      <GpuScheduler mode="status" />
      <div class="service-metrics">
        <Card><CardHeader><CardTitle>LLM</CardTitle></CardHeader><CardContent class="kv-list">
          <p><span>运行 / 排队</span><strong>{{ metricCount(serviceTaskMetrics.llm, 'running', 'cancelling') }} / {{ metricCount(serviceTaskMetrics.llm, 'pending', 'queued', 'retrying') }}</strong></p>
          <p><span>完成 / 失败</span><strong>{{ metricCount(serviceTaskMetrics.llm, 'succeeded') }} / {{ metricCount(serviceTaskMetrics.llm, 'failed', 'timeout') }}</strong></p>
          <p><span>Token / 并发时延 / TTFT / Prefill / Decode</span><strong>未采集</strong></p>
          <p><span>数据范围</span><strong>持久化任务汇总</strong></p>
        </CardContent></Card>
        <Card><CardHeader><CardTitle>TTS</CardTitle></CardHeader><CardContent class="kv-list">
          <p><span>运行 / 排队</span><strong>{{ metricCount(serviceTaskMetrics.tts, 'running', 'cancelling') }} / {{ metricCount(serviceTaskMetrics.tts, 'pending', 'queued', 'retrying') }}</strong></p>
          <p><span>完成 / 失败</span><strong>{{ metricCount(serviceTaskMetrics.tts, 'succeeded') }} / {{ metricCount(serviceTaskMetrics.tts, 'failed', 'timeout') }}</strong></p>
          <p><span>Batch / 字符 / 吞吐 / 平均耗时 / GPU</span><strong>未采集</strong></p>
          <p><span>数据范围</span><strong>持久化任务汇总</strong></p>
        </CardContent></Card>
        <Card><CardHeader><CardTitle>FFmpeg / 音频</CardTitle></CardHeader><CardContent class="kv-list">
          <p><span>运行 / 排队</span><strong>{{ metricCount(serviceTaskMetrics.ffmpeg, 'running', 'cancelling') }} / {{ metricCount(serviceTaskMetrics.ffmpeg, 'pending', 'queued', 'retrying') }}</strong></p>
          <p><span>完成 / 失败</span><strong>{{ metricCount(serviceTaskMetrics.ffmpeg, 'succeeded') }} / {{ metricCount(serviceTaskMetrics.ffmpeg, 'failed', 'timeout') }}</strong></p>
          <p><span>BGM 运行 / 排队</span><strong>{{ metricCount(serviceTaskMetrics.bgm, 'running', 'cancelling') }} / {{ metricCount(serviceTaskMetrics.bgm, 'pending', 'queued', 'retrying') }}</strong></p>
          <p><span>BGM 完成 / 失败</span><strong>{{ metricCount(serviceTaskMetrics.bgm, 'succeeded') }} / {{ metricCount(serviceTaskMetrics.bgm, 'failed', 'timeout') }}</strong></p>
        </CardContent></Card>
        <Card><CardHeader><CardTitle>API · 近 {{ Math.floor(performance.api.window_seconds / 60) }} 分钟</CardTitle></CardHeader><CardContent class="kv-list">
          <p><span>请求 / 5xx</span><strong>{{ performance.api.request_count }} / {{ performance.api.server_error_count }}</strong></p>
          <p><span>错误率</span><strong>{{ (performance.api.error_rate * 100).toFixed(2) }}%</strong></p>
          <p><span>平均 / P95</span><strong>{{ performance.api.average_ms ?? '—' }} / {{ performance.api.p95_ms ?? '—' }} ms</strong></p>
          <p><span>范围</span><strong>{{ performance.api.scope }}</strong></p>
        </CardContent></Card>
      </div>
      <Card><CardHeader><CardTitle>API 请求明细</CardTitle></CardHeader><CardContent>
        <AdminTable><thead><tr><th>路由</th><th>请求</th><th>5xx</th><th>错误率</th><th>平均</th><th>P95</th></tr></thead>
          <tbody><tr v-for="endpoint in performance.api.endpoints" :key="endpoint.route"><td class="mono">{{ endpoint.route }}</td><td>{{ endpoint.requests }}</td><td>{{ endpoint.server_errors }}</td><td>{{ (endpoint.error_rate * 100).toFixed(2) }}%</td><td>{{ endpoint.average_ms }} ms</td><td>{{ endpoint.p95_ms }} ms</td></tr></tbody></AdminTable>
        <p v-if="!performance.api.endpoints.length" class="admin-empty">此时间窗口暂无请求样本</p>
      </CardContent></Card>
      <Card><CardHeader><CardTitle>Worker 状态</CardTitle></CardHeader><CardContent>
        <AdminTable><thead><tr><th>Worker</th><th>状态</th><th>槽位</th><th>任务类型</th><th>最近心跳</th></tr></thead>
          <tbody><tr v-for="worker in performance.workers" :key="worker.worker_id"><td class="mono">{{ worker.worker_id }}</td><td><StatusPill :label="statusLabel(worker.status)" :tone="tone(worker.status)" /></td><td>{{ worker.capabilities.active_slots ?? 0 }} / {{ worker.capabilities.slots ?? '—' }}</td><td class="clip" :title="String(worker.capabilities.task_types ?? '')">{{ Array.isArray(worker.capabilities.task_types) ? worker.capabilities.task_types.join(' · ') : '—' }}</td><td>{{ date(worker.last_seen_at) }}</td></tr></tbody></AdminTable>
        <p v-if="!performance.workers.length" class="admin-empty">暂无 Worker 心跳</p>
      </CardContent></Card>
    </section>

    <section v-if="tab === 'users'" class="admin-section">
      <div class="admin-title"><p>{{ matchingUsers.length }} 位用户 · 每页 20 条</p>
        <div class="controls"><Input v-model="userSearch" aria-label="搜索用户" placeholder="搜索用户名或邮箱" class="search" />
          <select v-model="userRole" aria-label="用户角色"><option value="all">全部角色</option><option value="admin">管理员</option><option value="user">普通用户</option></select>
          <select v-model="userState" aria-label="账户状态"><option value="all">全部状态</option><option value="active">已启用</option><option value="disabled">已禁用</option></select>
          <select v-model="userSort" aria-label="用户排序"><option value="default">默认排序</option><option value="name">用户名</option><option value="recent">最近注册</option><option value="storage">存储占用最多</option></select>
          <Button v-if="userFiltered" variant="ghost" size="sm" @click="clearUserFilters">清空筛选</Button>
        </div>
      </div>
      <Card><CardContent class="admin-table-content"><AdminTable table-class="wide-table"><thead><tr><th>用户</th><th>角色 / 状态</th><th>注册 / 最近登录</th><th>使用量</th><th>项目 / 工作空间</th><th>存储占用</th><th class="user-actions">操作</th></tr></thead>
        <tbody><tr v-for="user in shownUsers" :key="user.id" :aria-selected="selectedUser?.id === user.id"><td><strong>{{ user.display_name || user.username }}</strong><small>{{ user.username }} · {{ user.email }}</small></td>
          <td><div class="badge-stack"><StatusPill :label="user.role === 'admin' ? '管理员' : '用户'" :tone="user.role === 'admin' ? 'positive' : 'neutral'" /><StatusPill :label="user.is_active ? '启用' : '禁用'" :tone="user.is_active ? 'positive' : 'negative'" /></div></td>
          <td>{{ date(user.created_at) }}<small>最近 {{ date(user.last_seen_at) }}</small></td>
          <td>{{ user.consumed_units ?? 0 }} 已用<small>{{ user.reserved_units ?? 0 }} 预留 · {{ user.available_units ?? 0 }} 可用</small></td>
          <td>{{ user.project_count ?? 0 }} 个项目</td>
          <td>{{ bytes(user.storage_bytes) }}<small>{{ user.project_file_count ?? '未采集' }} 个目录文件 · {{ user.file_count ?? 0 }} 个已登记</small></td>
          <td class="user-actions"><Button variant="outline" size="sm" @click="selectedUser = user">管理</Button></td></tr></tbody></AdminTable>
        <AdminEmptyState v-if="!shownUsers.length" title="没有匹配的用户" description="尝试其他用户名或邮箱，或清空搜索条件。"><Button v-if="userFiltered" variant="outline" size="sm" @click="clearUserFilters">清空搜索</Button></AdminEmptyState>
        <div v-if="matchingUsers.length" class="pager"><Button variant="outline" size="sm" :disabled="userPage <= 1" @click="userPage--">上一页</Button>{{ userPage }} / {{ userPages }}<Button variant="outline" size="sm" :disabled="userPage >= userPages" @click="userPage++">下一页</Button></div>
      </CardContent></Card>
      <AdminDrawer v-if="selectedUser" :title="selectedUser.username" @close="selectedUser = null"><div class="admin-form">
        <p v-if="error" role="alert" class="admin-error">{{ error }}</p>
        <p class="mono">{{ selectedUser.id }}</p><p>{{ selectedUser.email }} · {{ selectedUser.display_name || selectedUser.username }}</p>
        <p>项目：{{ selectedUser.project_count ?? 0 }} · 实际存储：{{ bytes(selectedUser.storage_bytes) }}（已登记文件 {{ bytes(selectedUser.file_bytes) }}）</p>
        <p>额度：{{ selectedUser.consumed_units ?? 0 }} 已用 · {{ selectedUser.reserved_units ?? 0 }} 预留 · {{ selectedUser.available_units ?? 0 }} 可用</p>
        <div class="controls"><Button variant="outline" :disabled="actionBusy || lastAdmin(selectedUser)" @click="runAction(async () => { if (selectedUser) await changeUser(selectedUser, { role: selectedUser.role === 'admin' ? 'user' : 'admin' }) })">{{ selectedUser.role === 'admin' ? '移除管理员' : '设为管理员' }}</Button><Button variant="outline" :disabled="actionBusy || lastAdmin(selectedUser)" @click="runAction(async () => { if (selectedUser) await changeUser(selectedUser, { is_active: !selectedUser.is_active }) })">{{ selectedUser.is_active ? '禁用用户' : '启用用户' }}</Button></div>
        <p v-if="lastAdmin(selectedUser)" class="admin-muted">此账户是最后一位启用的管理员，无法移除权限或禁用。</p>
        <div class="controls"><label for="quota-adjust">额度调整</label><Input id="quota-adjust" v-model="quotaAmount" type="number" placeholder="输入数量，正数增加 / 负数扣减" class="search" /><Button :disabled="actionBusy" @click="runAction(adjustQuota)">{{ actionBusy ? '处理中…' : '确认调整' }}</Button></div>
      </div></AdminDrawer>
    </section>

    <section v-if="tab === 'resources' && resources" class="admin-section">
      <div class="admin-title"><div><p>{{ resources.scope }}</p></div></div>
      <div class="metric-grid resource-metrics">
        <AdminStatCard label="磁盘已用 / 可用"><template #value>{{ bytes(resources.disk_used_bytes) }}</template>{{ bytes(resources.disk_free_bytes) }} 可用 · 共 {{ bytes(resources.disk_total_bytes) }}</AdminStatCard>
        <AdminStatCard label="项目存储占用"><template #value>{{ bytes(resources.project_storage?.size_bytes) }}</template>{{ resources.project_storage?.file_count ?? '未采集' }} 个文件</AdminStatCard>
        <AdminStatCard label="项目总数"><template #value>{{ resources.projects }}</template></AdminStatCard>
        <AdminStatCard label="公共音乐库"><template #value>{{ resources.music_library.count }} 首</template>{{ bytes(resources.music_library.size_bytes) }} · {{ resources.music_library.assigned_chapters ?? '—' }} 次章节指派</AdminStatCard>
      </div>
      <div class="overview-grid">
        <Card><CardHeader><CardTitle>工作空间文件分类</CardTitle></CardHeader><CardContent>
          <AdminTable table-class="resource-category-table"><thead><tr><th>类别</th><th>文件数</th><th>大小</th></tr></thead><tbody><tr v-for="row in resourceCategories" :key="row.kind"><td>{{ row.label }}</td><td>{{ row.count }}</td><td>{{ bytes(row.size_bytes) }}</td></tr></tbody></AdminTable>
          <p v-if="!resourceCategories.length" class="admin-empty">此 API 版本尚未提供工作空间扫描数据</p>
        </CardContent></Card>
        <Card><CardHeader><CardTitle>用户占用 · 前 20</CardTitle></CardHeader><CardContent>
          <AdminTable table-class="resource-users-table"><thead><tr><th>用户</th><th>工作空间</th><th>文件数</th><th>实际占用</th><th>登记文件</th></tr></thead>
            <tbody><tr v-for="row in resources.users" :key="row.username"><td>{{ row.username }}</td><td>{{ row.project_count ?? '未采集' }}</td><td>{{ resources.project_storage ? row.file_count ?? 0 : '未采集' }}</td><td>{{ resources.project_storage ? bytes(row.size_bytes) : '未采集' }}</td><td>{{ row.registered_file_count ?? row.count ?? '未采集' }}<small v-if="row.registered_file_bytes != null">{{ bytes(row.registered_file_bytes) }}</small></td></tr></tbody></AdminTable>
          <p v-if="!resources.users.length" class="admin-empty">暂无用户资源</p>
        </CardContent></Card>
      </div>
      <Card><CardHeader><CardTitle>临时文件清理</CardTitle></CardHeader><CardContent class="cleanup-row">
        <div><p><strong>{{ cleanupCandidates?.count ?? '未采集' }}</strong> 个超过 {{ cleanupCandidates?.older_than_days ?? 7 }} 天的临时文件 · {{ bytes(cleanupCandidates?.size_bytes) }}</p><small>仅清理不活跃工作空间中的普通临时文件；跳过特殊文件和正在运行任务的工作空间。</small></div>
        <Button variant="outline" :disabled="!cleanupCandidates?.count || loading || actionBusy" @click="runAction(cleanupTemp)"><Trash2 class="h-4 w-4" />清理过期临时文件</Button>
      </CardContent></Card>
      <p class="admin-muted">扫描范围：{{ resources.root_path }} · 模型或日志位于工作空间以外时，不包含在空间分类中。</p>
    </section>

    <section v-if="tab === 'settings'" class="admin-section admin-config">
      <nav class="settings-nav" aria-label="系统配置分类">
        <button v-for="item in ([['text','文本处理'],['models','解析与 LLM'],['audio','TTS 与音频'],['general','通用'],['storage','存储路径'],['runtime','Worker / Queue']] as [SettingsSection,string][])" :key="item[0]" :class="settingsSection === item[0] ? 'active' : ''" :aria-pressed="settingsSection === item[0]" @click="settingsSection = item[0]">{{ item[1] }}</button>
      </nav>
      <AdminSettings v-if="settingsSection === 'text' || settingsSection === 'models' || settingsSection === 'audio'" :section="settingsSection" />
      <GpuScheduler v-if="settingsSection === 'models'" mode="paths" />
      <GpuScheduler v-if="settingsSection === 'runtime'" mode="parameters" />
      <Card v-if="settingsSection === 'general'"><CardHeader><CardTitle>账户与客户端</CardTitle></CardHeader><CardContent>
        <div class="admin-setting-row">
          <div><h3>客户端功能日志</h3><p>统一控制实时日志和模型输出，已打开的客户端会自动同步。任务进度与失败提示继续显示。</p></div>
          <div class="admin-toggle-control"><span class="admin-toggle-feedback">即时保存</span><Switch aria-label="客户端功能日志" :model-value="clientDisplay.logsEnabled" :disabled="!clientDisplay.loaded || actionBusy" :busy="savingToggle === 'logs'" @update:model-value="saveToggle('logs')" /></div>
        </div>
        <div class="admin-setting-row">
          <div><h3>新用户注册</h3><p>控制登录页面是否允许新用户自行创建账户。</p></div>
          <div class="admin-toggle-control"><span class="admin-toggle-feedback">即时保存</span><Switch aria-label="允许新用户注册" :model-value="registration?.enabled ?? false" :disabled="!registration || loading || actionBusy" :busy="savingToggle === 'registration'" @update:model-value="saveToggle('registration')" /></div>
        </div>
        <div class="admin-setting-row">
          <div><label for="initial-quota">新用户初始额度</label><p>设置新账户获得的制作额度，必须为非负整数。</p></div>
          <div class="controls"><Input id="initial-quota" v-model="quotaDraft" type="number" min="0" class="w-28" /><Button variant="outline" size="sm" class="admin-quota-save" :disabled="!quota || actionBusy" @click="submitQuota">{{ savingQuota ? '保存中…' : '保存' }}</Button></div>
        </div>
      </CardContent></Card>
      <Card v-else-if="settingsSection === 'storage'"><CardHeader><CardTitle>存储路径</CardTitle></CardHeader><CardContent class="admin-form">
        <label for="storage-root">工作空间根目录</label><Input id="storage-root" v-model="rootDraft" class="mono" />
        <p class="admin-muted">当前路径 {{ storage?.root_path || '读取中' }} · 来源：{{ storage?.source === 'admin' ? '管理员配置' : '部署默认值' }}</p>
        <p class="admin-muted">更改时后端会迁移已登记的工作空间目录。</p><Button :disabled="!storage || actionBusy" @click="runAction(saveRoot)">{{ actionBusy ? '保存中…' : '保存存储根目录' }}</Button>
      </CardContent></Card>
      <Card v-else-if="settingsSection === 'runtime'"><CardHeader><CardTitle>Worker / Queue · 部署运行限制</CardTitle></CardHeader><CardContent class="kv-list">
        <template v-if="runtime"><p><span>任务租约</span><strong>{{ runtime.limits.task_lease_seconds }} 秒</strong></p><p><span>最大重试次数</span><strong>{{ runtime.limits.task_max_attempts }}</strong></p><p><span>上传大小上限</span><strong>{{ bytes(runtime.limits.max_upload_bytes) }}</strong></p><p><span>会话时长</span><strong>{{ runtime.limits.session_ttl_hours }} 小时</strong></p><p><span>Timeout / 并发限制</span><strong>未提供平台级配置接口</strong></p><p><span>配置来源</span><strong>部署环境 · 只读</strong></p></template>
        <p v-else class="admin-muted">此运行环境尚未提供非敏感运行限制数据。</p>
      </CardContent></Card>
    </section>

    <section v-if="tab === 'tasks'" class="admin-section">
      <div class="admin-title"><div><p>管理员运维视图 · 最近 {{ tasks.length }} 条</p></div>
        <div class="controls"><select v-model="taskStatus" aria-label="任务状态"><option value="all">全部状态</option><option value="queued">排队中</option><option value="running">运行中</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select><Input v-model="taskSearch" aria-label="搜索任务" placeholder="任务 ID、类型或用户" class="search" @keyup.enter="load" /><Button variant="outline" @click="load">筛选</Button></div>
      </div>
      <div class="metric-grid task-summary"><AdminStatCard label="排队中"><template #value>{{ metricCount(taskStatusSummary, 'pending', 'queued', 'retrying') }}</template></AdminStatCard><AdminStatCard label="运行中"><template #value>{{ metricCount(taskStatusSummary, 'running', 'cancelling') }}</template></AdminStatCard><AdminStatCard label="已完成"><template #value>{{ metricCount(taskStatusSummary, 'succeeded') }}</template></AdminStatCard><AdminStatCard label="失败 / 超时"><template #value>{{ metricCount(taskStatusSummary, 'failed', 'timeout') }}</template></AdminStatCard><AdminStatCard label="已取消"><template #value>{{ metricCount(taskStatusSummary, 'cancelled') }}</template></AdminStatCard></div>
      <Card><CardContent class="admin-table-content"><AdminTable table-class="wide-table"><thead><tr><th>类型 / ID</th><th>用户</th><th>项目</th><th>状态</th><th>Worker</th><th>创建时间</th><th class="user-actions">操作</th></tr></thead>
        <tbody><tr v-for="task in tasks" :key="task.id" :aria-selected="selectedTask?.id === task.id"><td><strong>{{ task.task_type }}</strong><small class="mono">{{ task.id }}</small></td><td>{{ task.owner_username }}</td><td class="mono">{{ task.project_id?.slice(0, 8) ?? '—' }}</td><td><StatusPill :label="statusLabel(task.status)" :tone="tone(task.status)" /></td><td class="clip" :title="task.worker_id ?? ''">{{ task.worker_id || '—' }}</td><td>{{ date(task.created_at) }}</td><td class="user-actions"><Button variant="outline" size="sm" @click="selectedTask = task">详情</Button></td></tr></tbody></AdminTable><AdminEmptyState v-if="!tasks.length" title="没有匹配的任务" description="调整状态或搜索条件后重新筛选。" /></CardContent></Card>
      <AdminDrawer v-if="selectedTask" title="任务详情" @close="selectedTask = null"><div class="admin-form">
        <p v-if="error" role="alert" class="admin-error">{{ error }}</p>
        <p class="mono">{{ selectedTask.id }}</p><p>{{ selectedTask.task_type }} · {{ selectedTask.owner_username }} · 项目 {{ selectedTask.project_id ?? '—' }}</p>
        <p>状态：{{ statusLabel(selectedTask.status) }} · 进度 {{ selectedTask.progress }}% · 第 {{ selectedTask.attempt_no ?? 0 }} 次尝试 · Worker {{ selectedTask.worker_id || '—' }}</p>
        <p>开始：{{ date(selectedTask.started_at) }} · 结束：{{ date(selectedTask.finished_at) }} · 耗时：{{ elapsed(selectedTask.started_at, selectedTask.finished_at) }}</p>
        <p v-if="selectedTask.error_message" class="admin-error">{{ selectedTask.error_code }} · {{ selectedTask.error_message }}</p>
        <div class="controls"><Button v-if="!['succeeded','failed','timeout','cancelled'].includes(selectedTask.status)" :disabled="actionBusy" variant="destructive" @click="runAction(async () => { if (selectedTask) await cancelTask(selectedTask) })">取消任务</Button><Button v-if="['failed','timeout','cancelled'].includes(selectedTask.status)" :disabled="actionBusy" variant="outline" @click="runAction(async () => { if (selectedTask) await retryTask(selectedTask) })"><RotateCcw class="h-4 w-4" />重新排队</Button></div>
      </div></AdminDrawer>
    </section>

    <section v-if="tab === 'logs'" class="admin-section">
      <div class="admin-title"><div><p>持久化任务失败、审计事件和 API 进程内 5xx；每次最多 50 条</p></div>
        <div class="controls"><select v-model="logHours" aria-label="时间范围"><option :value="1">近 1 小时</option><option :value="24">近 24 小时</option><option :value="168">近 7 天</option></select>
          <select v-model="logLevel" aria-label="日志级别"><option value="all">全部级别</option><option value="error">仅异常</option><option value="info">操作记录</option></select>
          <select v-model="logModule" aria-label="日志模块"><option value="all">全部模块</option><option v-for="name in ['system','api','llm','tts','worker','audio']" :key="name" :value="name">{{ name }}</option></select>
          <Input v-model="logSearch" aria-label="搜索日志" placeholder="搜索类型或消息" class="search" @keyup.enter="load" /><Button variant="outline" @click="load">筛选</Button>
        </div>
      </div>
      <Card><CardContent class="admin-table-content"><AdminTable><thead><tr><th>时间</th><th>级别</th><th>模块</th><th>类型</th><th>摘要</th></tr></thead>
        <tbody><tr v-for="entry in events" :key="entry.id"><td>{{ date(entry.time) }}</td><td><StatusPill :label="entry.level === 'error' ? '异常' : entry.level" :tone="entry.level === 'error' ? 'negative' : 'neutral'" /></td><td>{{ entry.module }}</td><td>{{ entry.type }}</td><td class="clip" :title="entry.message">{{ entry.message }}</td></tr></tbody></AdminTable><AdminEmptyState v-if="!events.length" title="当前范围内没有记录" description="尝试扩大时间范围或调整筛选条件。" /></CardContent></Card>
    </section>
    </div>
  </div>
</template>
