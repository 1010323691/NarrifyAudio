<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ArrowUpRight, RefreshCw, RotateCcw, Trash2 } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import CardContent from '@/components/ui/CardContent.vue'
import Input from '@/components/ui/Input.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useToast } from '@/components/ui/toast'
import * as api from '@/api/admin'

type Tab = 'overview' | 'performance' | 'users' | 'resources' | 'settings' | 'tasks' | 'logs'
type SettingsSection = 'general' | 'storage' | 'runtime' | 'llm' | 'tts' | 'audio'
const validTabs = new Set<Tab>(['overview', 'performance', 'users', 'resources', 'settings', 'tasks', 'logs'])
const route = useRoute()
const tab = computed<Tab>(() => validTabs.has(route.query.tab as Tab) ? route.query.tab as Tab : 'overview')
const { push: toast } = useToast()

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
const settingsSection = ref<SettingsSection>('general')
const rootDraft = ref('')
const quotaDraft = ref('0')
const registrationDraft = ref(true)
const userSearch = ref('')
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
let timer: ReturnType<typeof setInterval> | null = null

const matchingUsers = computed(() => users.value.filter(row =>
  `${row.username} ${row.display_name} ${row.email}`.toLowerCase().includes(userSearch.value.toLowerCase()),
))
const userPages = computed(() => Math.max(1, Math.ceil(matchingUsers.value.length / 20)))
const shownUsers = computed(() => matchingUsers.value.slice((userPage.value - 1) * 20, userPage.value * 20))

function aggregateTypes(prefixes: string[], exact: string[] = [], excluded: string[] = []) {
  const rows = performance.value?.tasks.by_type ?? []
  return rows.filter(row => !excluded.includes(row.task_type) && (prefixes.some(prefix => row.task_type.startsWith(prefix)) || exact.includes(row.task_type)))
    .reduce((total, row) => {
      for (const [status, count] of Object.entries(row.statuses)) total[status] = (total[status] ?? 0) + count
      return total
    }, {} as Record<string, number>)
}
const serviceTaskMetrics = computed(() => ({
  llm: aggregateTypes(['script.', 'music.']),
  tts: aggregateTypes(['tts.', 'voices.'], [], ['tts.merge']),
  ffmpeg: aggregateTypes(['audio.'], ['tts.merge']),
  bgm: aggregateTypes(['bgm.']),
}))
const taskStatusSummary = computed(() => taskMetrics.value?.status_counts ?? {})
const resourceCategories = computed(() => resources.value?.workspace_storage?.categories ?? [])
const cleanupCandidates = computed(() => resources.value?.workspace_storage?.cleanup_candidates)

watch(userSearch, () => { userPage.value = 1 })
function date(value?: string | null) { return value ? new Date(value).toLocaleString('zh-CN') : '—' }
function bytes(value?: number | null) {
  if (value == null) return '未采集'
  if (value < 1024) return `${value} B`
  const unit = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)))
  return `${(value / 1024 ** unit).toFixed(1)} ${['B', 'KB', 'MB', 'GB', 'TB'][unit]}`
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
  if (['warning', 'queued', 'pending', 'retrying', 'processing', 'running'].includes(status)) return 'warning'
  if (['error', 'failed', 'timeout', 'offline'].includes(status)) return 'negative'
  return 'neutral'
}
function statusLabel(status: string) {
  return ({ healthy: '正常', warning: '警告', error: '异常', unknown: '未采集', pending: '排队中', queued: '排队中',
    running: '运行中', processing: '运行中', paused: '已暂停', retrying: '重试中', succeeded: '已完成', completed: '已完成',
    failed: '失败', timeout: '超时', cancelled: '已取消', idle: '空闲', offline: '离线' } as Record<string, string>)[status] ?? status
}
async function load() {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    if (tab.value === 'overview') {
      const [current, metrics] = await Promise.all([api.getOverview(), api.getTaskMetrics()])
      overview.value = current
      taskMetrics.value = metrics
    }
    else if (tab.value === 'performance') performance.value = await api.getPerformance()
    else if (tab.value === 'users') users.value = await api.listUsers()
    else if (tab.value === 'resources') resources.value = await api.getResources()
    else if (tab.value === 'settings') {
      const [s, q, r] = await Promise.all([api.getStorageSettings(), api.getQuotaSettings(), api.getRegistrationSettings()])
      storage.value = s
      quota.value = q
      registration.value = r
      rootDraft.value = s.root_path
      quotaDraft.value = String(q.initial_units)
      registrationDraft.value = r.enabled
      runtime.value = await api.getRuntimeSettings().catch(() => null)
    } else if (tab.value === 'tasks') {
      const [rows, metrics] = await Promise.all([api.listTasks(taskStatus.value, taskSearch.value), api.getTaskMetrics()])
      tasks.value = rows
      taskMetrics.value = metrics
      if (selectedTask.value) selectedTask.value = rows.find(row => row.id === selectedTask.value?.id) ?? selectedTask.value
    } else {
      events.value = await api.getEvents(logLevel.value, logModule.value, logSearch.value, logHours.value)
    }
  } catch (cause: any) {
    error.value = cause?.message || String(cause)
  } finally {
    loading.value = false
  }
}
watch(tab, () => { selectedUser.value = null; selectedTask.value = null; void load() })
onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (document.visibilityState === 'visible' && ['overview', 'performance', 'tasks'].includes(tab.value)) void load()
  }, 15000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })

async function saveRegistration() {
  try { registration.value = await api.updateRegistrationSettings(registrationDraft.value); toast({ title: '注册设置已保存', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function saveQuota() {
  const value = Number(quotaDraft.value)
  if (!Number.isInteger(value) || value < 0) { error.value = '初始额度必须是非负整数'; return }
  try { quota.value = await api.updateQuotaSettings(value); toast({ title: '初始额度已保存', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function saveRoot() {
  if (!window.confirm('修改存储根目录会迁移已登记工作空间文件。确认继续？')) return
  try { storage.value = await api.updateStorageRoot(rootDraft.value); toast({ title: '存储根目录已保存', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
function lastAdmin(user: api.AdminUser) {
  return user.role === 'admin' && user.is_active && users.value.filter(row => row.role === 'admin' && row.is_active).length <= 1
}
async function changeUser(user: api.AdminUser, patch: { is_active?: boolean; role?: 'user' | 'admin' }) {
  if (!window.confirm(`确认更改 ${user.username} 的${patch.role ? '角色' : '状态'}？`)) return
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
  if (!window.confirm(`确认给 ${user.username} 调整 ${amount > 0 ? '+' : ''}${amount} 单位额度？`)) return
  try {
    await api.adjustQuota(user.id, amount, `admin-${user.id}-${Date.now()}`, '管理员后台调整')
    quotaAmount.value = ''
    users.value = await api.listUsers()
    selectedUser.value = users.value.find(row => row.id === user.id) ?? null
    toast({ title: '额度已调整', variant: 'success' })
  } catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function cancelTask(task: api.AdminTask) {
  if (!window.confirm(`确认取消任务 ${task.id}？`)) return
  try { await api.cancelTask(task.id); selectedTask.value = null; await load(); toast({ title: '取消请求已提交', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function retryTask(task: api.AdminTask) {
  if (!window.confirm(`将任务 ${task.id} 重新放入队列。确认重试？`)) return
  try { await api.retryTask(task.id); selectedTask.value = null; await load(); toast({ title: '任务已重新排队', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function cleanupTemp() {
  const candidates = cleanupCandidates.value
  if (!candidates?.count) return
  if (!window.confirm(`仅清理不活跃工作空间内超过 ${candidates.older_than_days} 天的 00_temp 普通文件，预计 ${candidates.count} 个、${bytes(candidates.size_bytes)}。此操作不可恢复，继续？`)) return
  try {
    const result = await api.cleanupStaleTemp()
    await load()
    toast({ title: `已清理 ${result.deleted_count} 个文件 · ${bytes(result.deleted_bytes)}`, variant: 'success' })
  } catch (cause: any) { error.value = cause?.message || String(cause) }
}
</script>

<template>
  <div class="admin-console">
    <header class="admin-head">
      <div>
        <span class="admin-eyebrow">ADMIN CONSOLE</span>
        <h1>管理后台</h1>
        <p>平台状态、用户、资源与运行运维</p>
      </div>
      <Button variant="outline" :disabled="loading" @click="load"><RefreshCw class="h-4 w-4" />刷新</Button>
    </header>
    <p v-if="error" class="admin-error" role="alert">{{ error }} <button @click="load">重试</button></p>
    <p v-if="loading && !overview && !performance && !users.length && !resources && !tasks.length && !events.length" class="admin-empty" role="status">正在加载管理数据…</p>

    <section v-if="tab === 'overview' && overview" class="admin-section">
      <div class="admin-title">
        <div><h2>系统总览</h2><p>更新时间 {{ date(overview.generated_at) }}</p></div>
        <StatusPill :label="overview.services.some(item => item.status === 'error') ? '存在异常' : overview.services.some(item => item.status === 'warning') ? '需要关注' : '运行正常'" :tone="overview.services.some(item => item.status === 'error') ? 'negative' : overview.services.some(item => item.status === 'warning') ? 'warning' : 'positive'" />
      </div>
      <div class="service-grid">
        <div v-for="service in overview.services" :key="service.key" class="service-row">
          <div><strong>{{ service.name }}</strong><StatusPill :label="statusLabel(service.status)" :tone="tone(service.status)" /></div>
          <p :title="service.detail">{{ service.detail }}</p>
        </div>
      </div>
      <div class="metric-grid">
        <div class="metric"><span>运行中任务</span><strong>{{ overview.tasks.running }}</strong></div>
        <div class="metric"><span>排队任务</span><strong>{{ overview.tasks.queued }}</strong></div>
        <div class="metric"><span>失败任务</span><strong :class="metricCount(taskMetrics?.status_counts ?? {}, 'failed', 'timeout') ? 'text-danger' : ''">{{ metricCount(taskMetrics?.status_counts ?? {}, 'failed', 'timeout') }}</strong></div>
        <div class="metric"><span>今日 API 请求</span><strong>{{ overview.today.api_requests ?? '未采集' }}</strong><small>{{ overview.today.api_requests_scope }}</small></div>
        <div class="metric"><span>活跃用户 · 15 分钟</span><strong>{{ overview.today.active_users }}</strong></div>
        <div class="metric"><span>今日 TTS 字符</span><strong>{{ overview.today.tts_characters ?? '未采集' }}</strong></div>
        <div class="metric"><span>今日 LLM Token</span><strong>{{ overview.today.llm_tokens ?? '未采集' }}</strong></div>
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
            <div v-if="overview.recent_errors.length" class="admin-table">
              <table class="compact-table"><thead><tr><th>时间</th><th>模块</th><th>类型</th><th>摘要</th></tr></thead>
                <tbody><tr v-for="entry in overview.recent_errors" :key="entry.id"><td>{{ date(entry.time) }}</td><td><StatusPill :label="entry.module" /></td><td>{{ entry.type }}</td><td class="clip" :title="entry.message">{{ entry.message }}</td></tr></tbody>
              </table>
            </div>
            <p v-else class="admin-muted">暂无近期异常记录</p>
            <RouterLink to="/admin?tab=logs">打开日志与异常 <ArrowUpRight class="inline h-3.5 w-3.5" /></RouterLink>
          </CardContent>
        </Card>
      </div>
    </section>

    <section v-if="tab === 'performance' && performance" class="admin-section">
      <div class="admin-title"><div><h2>服务与性能</h2><p>实时快照 · {{ date(performance.generated_at) }}</p></div></div>
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
        <div class="admin-table"><table><thead><tr><th>路由</th><th>请求</th><th>5xx</th><th>错误率</th><th>平均</th><th>P95</th></tr></thead>
          <tbody><tr v-for="endpoint in performance.api.endpoints" :key="endpoint.route"><td class="mono">{{ endpoint.route }}</td><td>{{ endpoint.requests }}</td><td>{{ endpoint.server_errors }}</td><td>{{ (endpoint.error_rate * 100).toFixed(2) }}%</td><td>{{ endpoint.average_ms }} ms</td><td>{{ endpoint.p95_ms }} ms</td></tr></tbody></table></div>
        <p v-if="!performance.api.endpoints.length" class="admin-empty">此时间窗口暂无请求样本</p>
      </CardContent></Card>
      <Card><CardHeader><CardTitle>Worker 状态</CardTitle></CardHeader><CardContent>
        <div class="admin-table"><table><thead><tr><th>Worker</th><th>状态</th><th>槽位</th><th>任务类型</th><th>最近心跳</th></tr></thead>
          <tbody><tr v-for="worker in performance.workers" :key="worker.worker_id"><td class="mono">{{ worker.worker_id }}</td><td><StatusPill :label="statusLabel(worker.status)" :tone="tone(worker.status)" /></td><td>{{ worker.capabilities.active_slots ?? 0 }} / {{ worker.capabilities.slots ?? '—' }}</td><td class="clip" :title="String(worker.capabilities.task_types ?? '')">{{ Array.isArray(worker.capabilities.task_types) ? worker.capabilities.task_types.join(' · ') : '—' }}</td><td>{{ date(worker.last_seen_at) }}</td></tr></tbody></table></div>
        <p v-if="!performance.workers.length" class="admin-empty">暂无 Worker 心跳</p>
      </CardContent></Card>
    </section>

    <section v-if="tab === 'users'" class="admin-section">
      <div class="admin-title"><div><h2>用户管理</h2><p>{{ matchingUsers.length }} 位用户 · 套餐字段当前未配置</p></div><Input v-model="userSearch" placeholder="搜索用户名或邮箱" class="search" /></div>
      <Card><CardContent class="pad"><div class="admin-table"><table class="wide-table"><thead><tr><th>用户</th><th>角色 / 状态</th><th>注册 / 最近登录</th><th>套餐</th><th>使用量</th><th>项目 / 工作空间</th><th>存储占用</th><th class="user-actions">操作</th></tr></thead>
        <tbody><tr v-for="user in shownUsers" :key="user.id"><td><strong>{{ user.display_name || user.username }}</strong><small>{{ user.username }} · {{ user.email }}</small></td>
          <td><div class="badge-stack"><StatusPill :label="user.role === 'admin' ? '管理员' : '用户'" :tone="user.role === 'admin' ? 'positive' : 'neutral'" /><StatusPill :label="user.is_active ? '启用' : '禁用'" :tone="user.is_active ? 'positive' : 'negative'" /></div></td>
          <td>{{ date(user.created_at) }}<small>最近 {{ date(user.last_seen_at) }}</small></td><td>未配置</td>
          <td>{{ user.consumed_units ?? 0 }} 已用<small>{{ user.reserved_units ?? 0 }} 预留 · {{ user.available_units ?? 0 }} 可用</small></td>
          <td>{{ user.project_count ?? 0 }} 项目<small>{{ user.workspace_count ?? '—' }} 工作空间</small></td>
          <td>{{ bytes(user.storage_bytes) }}<small>{{ user.workspace_file_count ?? '未采集' }} 个目录文件 · {{ user.file_count ?? 0 }} 个已登记</small></td>
          <td class="user-actions"><Button variant="outline" size="sm" @click="selectedUser = user">详情 / 操作</Button></td></tr></tbody></table></div>
        <p v-if="!shownUsers.length" class="admin-empty">没有匹配的用户</p>
        <div v-if="matchingUsers.length" class="pager"><Button variant="outline" size="sm" :disabled="userPage <= 1" @click="userPage--">上一页</Button>{{ userPage }} / {{ userPages }}<Button variant="outline" size="sm" :disabled="userPage >= userPages" @click="userPage++">下一页</Button></div>
      </CardContent></Card>
      <Card v-if="selectedUser"><CardHeader><CardTitle>用户详情 · {{ selectedUser.username }}</CardTitle></CardHeader><CardContent class="admin-form">
        <p class="mono">{{ selectedUser.id }}</p><p>{{ selectedUser.email }} · {{ selectedUser.display_name || selectedUser.username }}</p>
        <p>套餐：未配置 · 项目：{{ selectedUser.project_count ?? 0 }} · 工作空间：{{ selectedUser.workspace_count ?? '—' }} · 实际存储：{{ bytes(selectedUser.storage_bytes) }}（已登记文件 {{ bytes(selectedUser.file_bytes) }}）</p>
        <p>额度：{{ selectedUser.consumed_units ?? 0 }} 已用 · {{ selectedUser.reserved_units ?? 0 }} 预留 · {{ selectedUser.available_units ?? 0 }} 可用</p>
        <div class="controls"><Button variant="outline" :disabled="lastAdmin(selectedUser)" @click="changeUser(selectedUser, { role: selectedUser.role === 'admin' ? 'user' : 'admin' })">{{ selectedUser.role === 'admin' ? '移除管理员' : '设为管理员' }}</Button><Button variant="outline" :disabled="lastAdmin(selectedUser)" @click="changeUser(selectedUser, { is_active: !selectedUser.is_active })">{{ selectedUser.is_active ? '禁用用户' : '启用用户' }}</Button></div>
        <div class="controls"><label for="quota-adjust">额度调整</label><Input id="quota-adjust" v-model="quotaAmount" type="number" placeholder="正数增加 / 负数扣减" class="search" /><Button variant="outline" @click="adjustQuota">确认调整</Button></div>
      </CardContent></Card>
    </section>

    <section v-if="tab === 'resources' && resources" class="admin-section">
      <div class="admin-title"><div><h2>资源与存储</h2><p>{{ resources.scope }}</p></div></div>
      <div class="metric-grid resource-metrics">
        <div class="metric"><span>磁盘已用 / 可用</span><strong>{{ bytes(resources.disk_used_bytes) }}</strong><small>{{ bytes(resources.disk_free_bytes) }} 可用 · 共 {{ bytes(resources.disk_total_bytes) }}</small></div>
        <div class="metric"><span>工作空间实际占用</span><strong>{{ bytes(resources.workspace_storage?.size_bytes) }}</strong><small>{{ resources.workspace_storage?.file_count ?? '未采集' }} 个文件</small></div>
        <div class="metric"><span>工作空间 / 项目</span><strong>{{ resources.workspaces }} / {{ resources.projects }}</strong></div>
        <div class="metric"><span>公共音乐库</span><strong>{{ resources.music_library.count }} 首</strong><small>{{ bytes(resources.music_library.size_bytes) }} · {{ resources.music_library.assigned_chapters ?? '—' }} 次章节指派</small></div>
      </div>
      <div class="overview-grid">
        <Card><CardHeader><CardTitle>工作空间文件分类</CardTitle></CardHeader><CardContent>
          <div class="admin-table"><table><thead><tr><th>类别</th><th>文件数</th><th>大小</th></tr></thead><tbody><tr v-for="row in resourceCategories" :key="row.kind"><td>{{ row.label }}</td><td>{{ row.count }}</td><td>{{ bytes(row.size_bytes) }}</td></tr></tbody></table></div>
          <p v-if="!resourceCategories.length" class="admin-empty">此 API 版本尚未提供工作空间扫描数据</p>
        </CardContent></Card>
        <Card><CardHeader><CardTitle>用户占用 · 前 20</CardTitle></CardHeader><CardContent>
          <div class="admin-table"><table><thead><tr><th>用户</th><th>工作空间</th><th>文件数</th><th>实际占用</th><th>登记文件</th></tr></thead>
            <tbody><tr v-for="row in resources.users" :key="row.username"><td>{{ row.username }}</td><td>{{ row.workspace_count ?? '未采集' }}</td><td>{{ resources.workspace_storage ? row.file_count ?? 0 : '未采集' }}</td><td>{{ resources.workspace_storage ? bytes(row.size_bytes) : '未采集' }}</td><td>{{ row.registered_file_count ?? row.count ?? '未采集' }}<small v-if="row.registered_file_bytes != null">{{ bytes(row.registered_file_bytes) }}</small></td></tr></tbody></table></div>
          <p v-if="!resources.users.length" class="admin-empty">暂无用户资源</p>
        </CardContent></Card>
      </div>
      <Card><CardHeader><CardTitle>临时文件清理</CardTitle></CardHeader><CardContent class="cleanup-row">
        <div><p><strong>{{ cleanupCandidates?.count ?? '未采集' }}</strong> 个超过 {{ cleanupCandidates?.older_than_days ?? 7 }} 天的临时文件 · {{ bytes(cleanupCandidates?.size_bytes) }}</p><small>仅清理不活跃工作空间的 00_temp 普通文件；跳过符号链接和正在运行任务的工作空间。</small></div>
        <Button variant="outline" :disabled="!cleanupCandidates?.count || loading" @click="cleanupTemp"><Trash2 class="h-4 w-4" />清理过期临时文件</Button>
      </CardContent></Card>
      <p class="admin-muted">扫描范围：{{ resources.root_path }} · 模型或日志位于工作空间以外时，不包含在空间分类中。</p>
    </section>

    <section v-if="tab === 'settings'" class="admin-section">
      <div class="admin-title"><div><h2>系统配置</h2><p>只展示平台级配置；工作空间制作参数和密钥不在此处编辑。</p></div></div>
      <nav class="settings-nav" aria-label="配置模块">
        <button v-for="item in ([['general','通用'],['llm','LLM'],['tts','TTS'],['audio','音频 / FFmpeg'],['storage','存储路径'],['runtime','Worker / Queue']] as [SettingsSection,string][])" :key="item[0]" :class="settingsSection === item[0] ? 'active' : ''" @click="settingsSection = item[0]">{{ item[1] }}</button>
      </nav>
      <Card v-if="settingsSection === 'general'"><CardHeader><CardTitle>通用</CardTitle></CardHeader><CardContent class="admin-form">
        <label><input v-model="registrationDraft" type="checkbox" /> 允许新用户注册</label><Button :disabled="!registration" @click="saveRegistration">保存注册设置</Button>
        <label for="initial-quota">新用户初始额度</label><Input id="initial-quota" v-model="quotaDraft" type="number" min="0" class="search" /><Button :disabled="!quota" @click="saveQuota">保存初始额度</Button>
      </CardContent></Card>
      <Card v-else-if="settingsSection === 'storage'"><CardHeader><CardTitle>存储路径</CardTitle></CardHeader><CardContent class="admin-form">
        <label for="storage-root">工作空间根目录</label><Input id="storage-root" v-model="rootDraft" class="mono" />
        <p class="admin-muted">当前路径 {{ storage?.root_path || '读取中' }} · 来源：{{ storage?.source === 'admin' ? '管理员配置' : '部署默认值' }}</p>
        <p class="admin-muted">更改时后端会迁移已登记的工作空间目录。</p><Button :disabled="!storage" @click="saveRoot">保存存储根目录</Button>
      </CardContent></Card>
      <Card v-else-if="settingsSection === 'runtime'"><CardHeader><CardTitle>Worker / Queue · 部署运行限制</CardTitle></CardHeader><CardContent class="kv-list">
        <template v-if="runtime"><p><span>任务租约</span><strong>{{ runtime.limits.task_lease_seconds }} 秒</strong></p><p><span>最大重试次数</span><strong>{{ runtime.limits.task_max_attempts }}</strong></p><p><span>上传大小上限</span><strong>{{ bytes(runtime.limits.max_upload_bytes) }}</strong></p><p><span>会话时长</span><strong>{{ runtime.limits.session_ttl_hours }} 小时</strong></p><p><span>Timeout / 并发限制</span><strong>未提供平台级配置接口</strong></p><p><span>配置来源</span><strong>部署环境 · 只读</strong></p></template>
        <p v-else class="admin-muted">此运行环境尚未提供非敏感运行限制数据。</p>
      </CardContent></Card>
      <Card v-else-if="settingsSection === 'llm'"><CardHeader><CardTitle>LLM</CardTitle></CardHeader><CardContent class="kv-list">
        <p><span>配置归属</span><strong>{{ runtime ? (runtime.model_settings_scope === 'workspace' ? '工作空间' : '部署环境') : '未采集' }}</strong></p>
        <p><span>密钥 / 连接参数</span><strong>不从管理接口回显</strong></p>
        <p class="admin-muted">当前 Worker、队列和 API 性能见“服务与性能”；Token 与 TTFT 未采集。</p>
      </CardContent></Card>
      <Card v-else-if="settingsSection === 'tts'"><CardHeader><CardTitle>TTS</CardTitle></CardHeader><CardContent class="kv-list">
        <p><span>配置归属</span><strong>{{ runtime ? (runtime.model_settings_scope === 'workspace' ? '工作空间' : '部署环境') : '未采集' }}</strong></p>
        <p><span>引擎 / Worker 可用性</span><strong>见“服务与性能”监控</strong></p>
        <p><span>字符用量 / 默认合成参数</span><strong>字符未采集；参数按工作空间管理</strong></p>
      </CardContent></Card>
      <Card v-else><CardHeader><CardTitle>音频 / FFmpeg</CardTitle></CardHeader><CardContent class="kv-list">
        <p><span>服务可用性</span><strong>在服务与性能页读取 FFmpeg 检测结果</strong></p>
        <p><span>音频默认参数</span><strong>按工作空间保存</strong></p>
        <p class="admin-muted">此项目没有平台级音频默认值 API；页面不会显示模拟配置。</p>
      </CardContent></Card>
    </section>

    <section v-if="tab === 'tasks'" class="admin-section">
      <div class="admin-title"><div><h2>任务 / 队列</h2><p>管理员运维视图 · 最近 {{ tasks.length }} 条</p></div>
        <div class="controls"><select v-model="taskStatus" aria-label="任务状态"><option value="all">全部状态</option><option value="queued">排队中</option><option value="running">运行中</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select><Input v-model="taskSearch" placeholder="任务 ID、类型或用户" class="search" @keyup.enter="load" /><Button variant="outline" @click="load">筛选</Button></div>
      </div>
      <div class="metric-grid task-summary"><div class="metric"><span>排队中</span><strong>{{ metricCount(taskStatusSummary, 'pending', 'queued', 'retrying') }}</strong></div><div class="metric"><span>运行中</span><strong>{{ metricCount(taskStatusSummary, 'running', 'cancelling', 'paused') }}</strong></div><div class="metric"><span>已完成</span><strong>{{ metricCount(taskStatusSummary, 'succeeded') }}</strong></div><div class="metric"><span>失败 / 超时</span><strong>{{ metricCount(taskStatusSummary, 'failed', 'timeout') }}</strong></div><div class="metric"><span>已取消</span><strong>{{ metricCount(taskStatusSummary, 'cancelled') }}</strong></div></div>
      <Card><CardContent class="pad"><div class="admin-table"><table class="wide-table"><thead><tr><th>类型 / ID</th><th>用户</th><th>项目</th><th>状态</th><th>Worker</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody><tr v-for="task in tasks" :key="task.id"><td><strong>{{ task.task_type }}</strong><small class="mono">{{ task.id }}</small></td><td>{{ task.owner_username }}</td><td class="mono">{{ task.project_id?.slice(0, 8) ?? '—' }}</td><td><StatusPill :label="statusLabel(task.status)" :tone="tone(task.status)" /></td><td class="clip" :title="task.worker_id ?? ''">{{ task.worker_id || '—' }}</td><td>{{ date(task.created_at) }}</td><td><Button variant="outline" size="sm" @click="selectedTask = task">详情</Button></td></tr></tbody></table></div><p v-if="!tasks.length" class="admin-empty">没有匹配的任务</p></CardContent></Card>
      <Card v-if="selectedTask"><CardHeader><CardTitle>任务详情</CardTitle></CardHeader><CardContent class="admin-form">
        <p class="mono">{{ selectedTask.id }}</p><p>{{ selectedTask.task_type }} · {{ selectedTask.owner_username }} · 项目 {{ selectedTask.project_id ?? '—' }}</p>
        <p>状态：{{ statusLabel(selectedTask.status) }} · 进度 {{ selectedTask.progress }}% · 第 {{ selectedTask.attempt_no ?? 0 }} 次尝试 · Worker {{ selectedTask.worker_id || '—' }}</p>
        <p>开始：{{ date(selectedTask.started_at) }} · 结束：{{ date(selectedTask.finished_at) }} · 耗时：{{ elapsed(selectedTask.started_at, selectedTask.finished_at) }}</p>
        <p v-if="selectedTask.error_message" class="admin-error">{{ selectedTask.error_code }} · {{ selectedTask.error_message }}</p>
        <div class="controls"><Button v-if="!['succeeded','failed','timeout','cancelled'].includes(selectedTask.status)" variant="outline" @click="cancelTask(selectedTask)">取消任务</Button><Button v-if="['failed','timeout','cancelled'].includes(selectedTask.status)" variant="outline" @click="retryTask(selectedTask)"><RotateCcw class="h-4 w-4" />重新排队</Button></div>
      </CardContent></Card>
    </section>

    <section v-if="tab === 'logs'" class="admin-section">
      <div class="admin-title"><div><h2>日志与异常</h2><p>持久化任务失败、审计事件和 API 进程内 5xx；每次最多 50 条</p></div>
        <div class="controls"><select v-model="logHours" aria-label="时间范围"><option :value="1">近 1 小时</option><option :value="24">近 24 小时</option><option :value="168">近 7 天</option></select>
          <select v-model="logLevel" aria-label="日志级别"><option value="all">全部级别</option><option value="error">仅异常</option><option value="info">操作记录</option></select>
          <select v-model="logModule" aria-label="日志模块"><option value="all">全部模块</option><option v-for="name in ['system','api','llm','tts','worker','audio']" :key="name" :value="name">{{ name }}</option></select>
          <Input v-model="logSearch" placeholder="搜索类型或消息" class="search" @keyup.enter="load" /><Button variant="outline" @click="load">筛选</Button>
        </div>
      </div>
      <Card><CardContent class="pad"><div class="admin-table"><table><thead><tr><th>时间</th><th>级别</th><th>模块</th><th>类型</th><th>摘要</th></tr></thead>
        <tbody><tr v-for="entry in events" :key="entry.id"><td>{{ date(entry.time) }}</td><td><StatusPill :label="entry.level === 'error' ? '异常' : entry.level" :tone="entry.level === 'error' ? 'negative' : 'neutral'" /></td><td>{{ entry.module }}</td><td>{{ entry.type }}</td><td class="clip" :title="entry.message">{{ entry.message }}</td></tr></tbody></table></div><p v-if="!events.length" class="admin-empty">没有匹配的记录</p></CardContent></Card>
    </section>
  </div>
</template>

<style scoped>
.admin-console{max-width:1500px;min-width:0;margin:auto;padding-bottom:32px}.admin-head,.admin-title{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}.admin-head h1{font-size:26px;font-weight:750;line-height:1.25}.admin-head p,.admin-title p{margin-top:4px;color:hsl(var(--muted-foreground));font-size:13px}.admin-eyebrow{font-size:10px;font-weight:800;letter-spacing:.13em;color:hsl(var(--primary))}.admin-section{display:grid;gap:16px}.admin-title h2{font-size:19px;font-weight:750}.service-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:9px}.service-row{min-width:0;border:1px solid hsl(var(--border));border-radius:10px;background:hsl(var(--card));padding:11px 12px}.service-row>div{display:flex;align-items:center;justify-content:space-between;gap:6px}.service-row strong{font-size:12px}.service-row p{margin-top:7px;overflow:hidden;color:hsl(var(--muted-foreground));font-size:11px;text-overflow:ellipsis;white-space:nowrap}.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:9px}.metric{min-width:0;border:1px solid hsl(var(--border));border-radius:10px;background:hsl(var(--card));padding:12px 14px}.metric span{display:block;color:hsl(var(--muted-foreground));font-size:11px}.metric strong{display:block;margin-top:5px;font-size:21px;font-weight:750;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.metric small{display:block;margin-top:4px;color:hsl(var(--muted-foreground));font-size:10px;line-height:1.4}.overview-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.overview-grid>*{min-width:0}.service-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.service-metrics>*{min-width:0}.kv-list{display:grid;gap:9px;font-size:12px}.kv-list p{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid hsl(var(--border));padding-bottom:8px}.kv-list p:last-child{border-bottom:0;padding-bottom:0}.kv-list span,.admin-muted{color:hsl(var(--muted-foreground))}.kv-list strong{text-align:right;font-variant-numeric:tabular-nums}.admin-muted{font-size:12px;line-height:1.5}.admin-console a{display:inline-flex;align-items:center;gap:4px;margin-top:10px;color:hsl(var(--primary));font-size:12px;font-weight:650}.gpu-block+.gpu-block{margin-top:18px;padding-top:14px;border-top:1px solid hsl(var(--border))}.gpu-block h3{margin-bottom:10px;font-size:13px;font-weight:700}.admin-error{border:1px solid hsl(var(--destructive)/.3);border-radius:8px;background:hsl(var(--destructive)/.08);padding:10px;color:hsl(var(--destructive));font-size:12px;overflow-wrap:anywhere}.admin-error button{text-decoration:underline}.text-danger{color:hsl(var(--destructive))}.controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.controls select{min-height:36px;max-width:160px;border:1px solid hsl(var(--border));border-radius:7px;background:hsl(var(--background));padding:0 8px;font-size:13px}.search{width:215px;max-width:100%}.pad{padding-top:16px}.admin-table{width:100%;overflow-x:auto}table{width:100%;min-width:650px;border-collapse:collapse;font-size:12px}.wide-table{min-width:1050px}.compact-table{min-width:500px}th{text-align:left;color:hsl(var(--muted-foreground));font-weight:650;white-space:nowrap}th,td{padding:10px 9px;border-bottom:1px solid hsl(var(--border));vertical-align:middle}td{max-width:300px;overflow-wrap:anywhere}td small{display:block;margin-top:3px;color:hsl(var(--muted-foreground));font-size:10px}tr:last-child td{border-bottom:0}.clip{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}.admin-empty{padding:24px;text-align:center;color:hsl(var(--muted-foreground));font-size:13px}.pager{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:12px;font-size:12px}.admin-form{display:grid;justify-items:start;gap:12px;font-size:13px}.admin-form>*{max-width:100%}.admin-form input[type=checkbox]{width:16px;height:16px;margin-right:5px;accent-color:hsl(var(--primary))}.badge-stack{display:flex;flex-direction:column;align-items:flex-start;gap:4px}.cleanup-row{display:flex;align-items:center;justify-content:space-between;gap:18px}.cleanup-row p{font-size:13px}.cleanup-row small{display:block;margin-top:5px;color:hsl(var(--muted-foreground));font-size:11px}.settings-nav{display:flex;gap:7px;overflow-x:auto;border-bottom:1px solid hsl(var(--border));padding-bottom:8px}.settings-nav button{white-space:nowrap;border:1px solid transparent;border-radius:8px;padding:8px 11px;color:hsl(var(--muted-foreground));font-size:12px;font-weight:650}.settings-nav button.active{border-color:hsl(var(--border));background:hsl(var(--card));color:hsl(var(--foreground));box-shadow:0 1px 2px hsl(var(--foreground)/.05)}.task-summary{grid-template-columns:repeat(5,minmax(120px,1fr))}@media(max-width:900px){.overview-grid,.service-metrics{grid-template-columns:1fr}.task-summary{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:600px){.admin-head h1{font-size:22px}.service-grid,.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.task-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.admin-title{align-items:stretch}.controls{width:100%}.controls .search{flex:1 1 150px}.cleanup-row{align-items:flex-start;flex-direction:column}.settings-nav button{padding:7px 9px}}
.metric-grid{grid-template-columns:repeat(4,minmax(0,1fr))}
@media(max-width:900px){.metric-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
.admin-section>*{min-width:0;max-width:100%}.user-actions{position:sticky;right:0;background:hsl(var(--card));box-shadow:-8px 0 10px hsl(var(--foreground)/.04)}
</style>
