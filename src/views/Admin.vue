<script setup lang="ts">
import { computed, onActivated, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { RefreshCw } from 'lucide-vue-next'
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
const validTabs = new Set<Tab>(['overview', 'performance', 'users', 'resources', 'settings', 'tasks', 'logs'])
const route = useRoute()
const tab = computed<Tab>(() => validTabs.has(route.query.tab as Tab) ? route.query.tab as Tab : 'overview')
const { push: toast } = useToast()
const overview = ref<api.AdminOverview | null>(null)
const performance = ref<api.AdminPerformance | null>(null)
const resources = ref<api.AdminResources | null>(null)
const users = ref<api.AdminUser[]>([])
const tasks = ref<api.AdminTask[]>([])
const events = ref<api.AdminEvent[]>([])
const storage = ref<api.StorageSettings | null>(null)
const quota = ref<api.QuotaSettings | null>(null)
const registration = ref<api.RegistrationSettings | null>(null)
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

const matchingUsers = computed(() => users.value.filter(row => `${row.username} ${row.display_name} ${row.email}`.toLowerCase().includes(userSearch.value.toLowerCase())))
const userPages = computed(() => Math.max(1, Math.ceil(matchingUsers.value.length / 20)))
const shownUsers = computed(() => matchingUsers.value.slice((userPage.value - 1) * 20, userPage.value * 20))
const serviceTaskMetrics = computed(() => {
  const rows = performance.value?.tasks.by_type ?? []
  const sum = (matches: (taskType: string) => boolean) => rows.filter(row => matches(row.task_type)).reduce((total, row) => {
    for (const [status, count] of Object.entries(row.statuses)) total[status] = (total[status] ?? 0) + count
    return total
  }, {} as Record<string, number>)
  return {
    llm: sum(type => type.startsWith('script.')),
    tts: sum(type => type.startsWith('tts.') && type !== 'tts.merge'),
    audio: sum(type => type.startsWith('audio.') || type.startsWith('bgm.') || type === 'tts.merge'),
  }
})
function metricCount(statuses: Record<string, number>, ...keys: string[]) { return keys.reduce((total, key) => total + (statuses[key] ?? 0), 0) }
watch(userSearch, () => { userPage.value = 1 })
function date(value?: string | null) { return value ? new Date(value).toLocaleString('zh-CN') : '—' }
function elapsed(start?: string | null, end?: string | null) {
  if (!start) return '—'
  const seconds = Math.max(0, Math.floor(((end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime()) / 1000))
  return seconds < 60 ? `${seconds} 秒` : seconds < 3600 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`
}
function bytes(value?: number | null) {
  if (value == null) return '未采集'
  if (value < 1024) return `${value} B`
  const unit = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)))
  return `${(value / 1024 ** unit).toFixed(1)} ${['B', 'KB', 'MB', 'GB', 'TB'][unit]}`
}
function tone(status: string): 'positive' | 'warning' | 'negative' | 'neutral' {
  if (['healthy', 'succeeded', 'idle'].includes(status)) return 'positive'
  if (['warning', 'queued', 'pending', 'retrying'].includes(status)) return 'warning'
  if (['error', 'failed', 'timeout', 'offline'].includes(status)) return 'negative'
  return 'neutral'
}
function statusLabel(status: string) { return ({ healthy: '正常', warning: '警告', error: '异常', unknown: '未采集' } as Record<string, string>)[status] ?? status }
async function load(silent = false) {
  if (loading.value) return
  if (!silent) loading.value = true
  error.value = ''
  try {
    if (tab.value === 'overview') overview.value = await api.getOverview()
    else if (tab.value === 'performance') performance.value = await api.getPerformance()
    else if (tab.value === 'users') users.value = await api.listUsers()
    else if (tab.value === 'resources') resources.value = await api.getResources()
    else if (tab.value === 'settings') {
      const [s, q, r] = await Promise.all([api.getStorageSettings(), api.getQuotaSettings(), api.getRegistrationSettings()])
      storage.value = s; quota.value = q; registration.value = r
      rootDraft.value = s.root_path; quotaDraft.value = String(q.initial_units); registrationDraft.value = r.enabled
    } else if (tab.value === 'tasks') tasks.value = await api.listTasks(taskStatus.value, taskSearch.value)
    else events.value = await api.getEvents(logLevel.value, logModule.value, logSearch.value, logHours.value)
  } catch (cause: any) { error.value = cause?.message || String(cause) }
  finally { loading.value = false }
}
watch(tab, () => { selectedUser.value = null; selectedTask.value = null; void load() })
onMounted(() => { void load(); timer = setInterval(() => {
  if (document.visibilityState === 'visible' && ['overview', 'performance', 'tasks'].includes(tab.value)) void load(true)
}, 10000) })
onActivated(() => { void load(true) })
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
  if (!window.confirm('修改存储根目录将迁移已登记工作空间文件。确认继续？')) return
  try { storage.value = await api.updateStorageRoot(rootDraft.value); toast({ title: '存储根目录已保存', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
function lastAdmin(user: api.AdminUser) { return user.role === 'admin' && user.is_active && users.value.filter(row => row.role === 'admin' && row.is_active).length <= 1 }
async function changeUser(user: api.AdminUser, patch: { is_active?: boolean; role?: 'user' | 'admin' }) {
  if (!window.confirm(`确认更改 ${user.username} 的${patch.role ? '角色' : '状态'}？`)) return
  try { const result = await api.updateUser(user.id, patch); user.role = result.role; user.is_active = result.is_active; toast({ title: '用户已更新', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function adjustQuota() {
  const user = selectedUser.value
  const amount = Number(quotaAmount.value)
  if (!user || !Number.isInteger(amount) || amount === 0) { error.value = '请输入非零整数额度'; return }
  if (!window.confirm(`确认给 ${user.username} 调整 ${amount > 0 ? '+' : ''}${amount} 单位额度？`)) return
  try { await api.adjustQuota(user.id, amount, `admin-${user.id}-${Date.now()}`, '管理员后台调整'); quotaAmount.value = ''; toast({ title: '额度已调整', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
async function cancelTask(task: api.AdminTask) {
  if (!window.confirm(`确认取消任务 ${task.id}？`)) return
  try { const result = await api.cancelTask(task.id); task.status = result.status; toast({ title: '取消请求已提交', variant: 'success' }) }
  catch (cause: any) { error.value = cause?.message || String(cause) }
}
</script>

<template>
  <div class="admin-console">
    <header class="admin-head"><div><span class="admin-eyebrow">ADMIN CONSOLE</span><h1>管理后台</h1><p>系统管理、资源与运行监控</p></div><Button variant="outline" :disabled="loading" @click="load()"><RefreshCw class="h-4 w-4" />刷新</Button></header>
    <p v-if="error" class="admin-error" role="alert">{{ error }} <button @click="load()">重试</button></p>
    <p v-if="loading && !overview && !performance && !users.length && !resources" class="admin-empty" role="status">正在加载管理数据…</p>

    <section v-if="tab === 'overview' && overview" class="admin-section"><div class="admin-title"><div><h2>系统状态</h2><p>更新时间 {{ date(overview.generated_at) }}</p></div><StatusPill :label="overview.services.some(s => s.status === 'error') ? '存在异常' : overview.services.some(s => s.status === 'warning') ? '需要关注' : '运行正常'" :tone="overview.services.some(s => s.status === 'error') ? 'negative' : overview.services.some(s => s.status === 'warning') ? 'warning' : 'positive'" /></div>
      <div class="admin-services"><div v-for="service in overview.services" :key="service.key" class="admin-service"><div><strong>{{ service.name }}</strong><StatusPill :label="statusLabel(service.status)" :tone="tone(service.status)" /></div><p :title="service.detail">{{ service.detail }}</p></div></div>
      <div class="admin-stats"><div><span>运行中</span><strong>{{ overview.tasks.running }}</strong></div><div><span>等待调度</span><strong>{{ overview.tasks.queued }}</strong></div><div><span>今日完成</span><strong>{{ overview.today.completed }}</strong></div><div><span>今日失败</span><strong>{{ overview.today.failed }}</strong></div><div><span>活跃会话用户</span><strong>{{ overview.today.active_users }}</strong></div><div><span>用户总数</span><strong>{{ overview.user_count }}</strong></div></div>
      <div class="admin-grid"><Card><CardHeader><CardTitle>性能摘要</CardTitle></CardHeader><CardContent class="admin-kv"><p><span>Worker 槽位</span><strong>{{ overview.workers.active_slots }} / {{ overview.workers.total_slots }}</strong></p><p><span>队列连接</span><StatusPill :label="overview.queue.available ? '正常' : '异常'" :tone="overview.queue.available ? 'positive' : 'negative'" /></p><p><span>宿主 CPU / 服务内存</span><strong>{{ overview.system.cpu_percent == null ? '未采集' : `${overview.system.cpu_percent}%` }} / {{ bytes(overview.system.ram_used_bytes) }} / {{ bytes(overview.system.ram_total_bytes) }}</strong></p><p><span>API 近 5 分钟请求 / 5xx</span><strong>{{ overview.api.request_count }} / {{ overview.api.server_error_count }}</strong></p><p><span>GPU</span><strong>{{ overview.gpu.length ? overview.gpu.map(g => `${g.index}: ${g.utilization_percent}%`).join(' · ') : '未采集' }}</strong></p><RouterLink to="/admin?tab=performance">查看性能监控 →</RouterLink></CardContent></Card><Card><CardHeader><CardTitle>最近异常</CardTitle></CardHeader><CardContent><p v-if="!overview.recent_errors.length" class="admin-muted">暂无最近任务异常</p><div v-for="entry in overview.recent_errors" :key="entry.id" class="admin-event"><span>{{ date(entry.time) }}</span><strong>{{ entry.module }} · {{ entry.type }}</strong><p :title="entry.message">{{ entry.message }}</p></div><RouterLink to="/admin?tab=logs">查看日志与异常 →</RouterLink></CardContent></Card></div>
    </section>

    <section v-if="tab === 'performance' && performance" class="admin-section"><div class="admin-title"><div><h2>性能监控</h2><p>实时快照 · {{ date(performance.generated_at) }}</p></div></div>
      <div class="admin-grid"><Card><CardHeader><CardTitle>系统资源</CardTitle></CardHeader><CardContent class="admin-kv"><p><span>宿主 CPU</span><strong>{{ performance.system.cpu_percent == null ? '未采集' : `${performance.system.cpu_percent}%` }}</strong></p><p><span>服务容器内存</span><strong>{{ bytes(performance.system.ram_used_bytes) }} / {{ bytes(performance.system.ram_total_bytes) }}</strong></p><p><span>磁盘</span><strong>{{ bytes(performance.system.disk_used_bytes) }} / {{ bytes(performance.system.disk_total_bytes) }}</strong></p><p><span>运行时间</span><strong>{{ performance.system.uptime_seconds == null ? '未采集' : `${Math.floor(performance.system.uptime_seconds / 3600)} 小时` }}</strong></p></CardContent></Card><Card><CardHeader><CardTitle>GPU</CardTitle></CardHeader><CardContent><p v-if="!performance.gpu.length" class="admin-muted">未采集到 GPU 数据</p><div v-for="gpu in performance.gpu" :key="gpu.index" class="admin-kv"><h3>{{ gpu.index }} · {{ gpu.name }}</h3><p><span>利用率</span><strong>{{ gpu.utilization_percent }}%</strong></p><p><span>显存</span><strong>{{ gpu.memory_used_mb }} / {{ gpu.memory_total_mb }} MB</strong></p><p><span>温度</span><strong>{{ gpu.temperature_c }} °C</strong></p></div></CardContent></Card></div>
      <div class="admin-grid"><Card><CardHeader><CardTitle>任务与队列</CardTitle></CardHeader><CardContent class="admin-kv"><p><span>运行 / 排队</span><strong>{{ performance.tasks.stage_counts.consuming }} / {{ performance.tasks.stage_counts.queued + performance.tasks.stage_counts.production }}</strong></p><p><span>近一分钟提交 / 完成 / 失败</span><strong>{{ performance.tasks.throughput_60s.submitted }} / {{ performance.tasks.throughput_60s.completed }} / {{ performance.tasks.throughput_60s.failed }}</strong></p><p><span>Worker / 槽位</span><strong>{{ performance.tasks.worker_pool.online_workers }} / {{ performance.tasks.worker_pool.active_slots }} / {{ performance.tasks.worker_pool.total_slots }}</strong></p><p><span>Redis 消息 / 待确认</span><strong>{{ performance.queue.length }} / {{ performance.queue.pending }}</strong></p></CardContent></Card><Card><CardHeader><CardTitle>LLM 任务</CardTitle></CardHeader><CardContent class="admin-kv"><p><span>运行 / 排队</span><strong>{{ metricCount(serviceTaskMetrics.llm, 'running', 'cancelling') }} / {{ metricCount(serviceTaskMetrics.llm, 'queued', 'pending', 'retrying') }}</strong></p><p><span>已完成 / 失败</span><strong>{{ metricCount(serviceTaskMetrics.llm, 'succeeded') }} / {{ metricCount(serviceTaskMetrics.llm, 'failed', 'timeout') }}</strong></p><p class="admin-muted">按 script.* 持久化任务统计；Token、TTFT 和吞吐未采集。</p></CardContent></Card><Card><CardHeader><CardTitle>TTS 任务</CardTitle></CardHeader><CardContent class="admin-kv"><p><span>运行 / 排队</span><strong>{{ metricCount(serviceTaskMetrics.tts, 'running', 'cancelling') }} / {{ metricCount(serviceTaskMetrics.tts, 'queued', 'pending', 'retrying') }}</strong></p><p><span>已完成 / 失败</span><strong>{{ metricCount(serviceTaskMetrics.tts, 'succeeded') }} / {{ metricCount(serviceTaskMetrics.tts, 'failed', 'timeout') }}</strong></p><p class="admin-muted">按 tts.* 持久化任务统计；字符数、实时倍率和平均耗时未采集。</p></CardContent></Card><Card><CardHeader><CardTitle>音频处理任务</CardTitle></CardHeader><CardContent class="admin-kv"><p><span>运行 / 排队</span><strong>{{ metricCount(serviceTaskMetrics.audio, 'running', 'cancelling') }} / {{ metricCount(serviceTaskMetrics.audio, 'queued', 'pending', 'retrying') }}</strong></p><p><span>已完成 / 失败</span><strong>{{ metricCount(serviceTaskMetrics.audio, 'succeeded') }} / {{ metricCount(serviceTaskMetrics.audio, 'failed', 'timeout') }}</strong></p><p class="admin-muted">汇总 audio.*、bgm.* 与 TTS 合并任务。</p></CardContent></Card></div>
      <Card><CardHeader><CardTitle>API · 近 {{ Math.round(performance.api.window_seconds / 60) }} 分钟</CardTitle></CardHeader><CardContent class="admin-section"><div class="admin-stats"><div><span>请求数</span><strong>{{ performance.api.request_count }}</strong></div><div><span>5xx 错误</span><strong>{{ performance.api.server_error_count }}</strong></div><div><span>错误率</span><strong>{{ (performance.api.error_rate * 100).toFixed(2) }}%</strong></div><div><span>平均 / P95</span><strong>{{ performance.api.average_ms ?? '—' }} / {{ performance.api.p95_ms ?? '—' }} ms</strong></div></div><div class="admin-table"><table><thead><tr><th>Endpoint</th><th>请求</th><th>5xx</th><th>平均</th><th>P95</th></tr></thead><tbody><tr v-for="endpoint in performance.api.endpoints" :key="endpoint.route"><td class="mono">{{ endpoint.route }}</td><td>{{ endpoint.requests }}</td><td>{{ endpoint.server_errors }}</td><td>{{ endpoint.average_ms }} ms</td><td>{{ endpoint.p95_ms }} ms</td></tr></tbody></table></div><p class="admin-muted">{{ performance.api.scope }}。状态码分布：{{ Object.entries(performance.api.status_counts).map(([code, count]) => code + ': ' + count).join(' · ') || '暂无请求样本' }}</p></CardContent></Card>
<p class="admin-muted">未采集指标：{{ performance.unavailable_metrics.join("、") }}。GPU 数据仅在 API 主机可访问 nvidia-smi 时提供。</p>
      <Card><CardHeader><CardTitle>Worker 状态</CardTitle></CardHeader><CardContent><div class="admin-table"><table><thead><tr><th>Worker</th><th>状态</th><th>槽位</th><th>任务类型</th><th>最近心跳</th></tr></thead><tbody><tr v-for="worker in performance.workers" :key="worker.worker_id"><td class="mono">{{ worker.worker_id }}</td><td><StatusPill :label="worker.status" :tone="tone(worker.status)" /></td><td>{{ worker.capabilities.active_slots ?? (worker.current_task_id ? 1 : 0) }} / {{ worker.capabilities.slots ?? 1 }}</td><td class="clip" :title="String(worker.capabilities.task_types ?? '')">{{ Array.isArray(worker.capabilities.task_types) ? worker.capabilities.task_types.join(' · ') : '—' }}</td><td>{{ date(worker.last_seen_at) }}</td></tr></tbody></table></div><p v-if="!performance.workers.length" class="admin-empty">暂无 Worker 心跳</p></CardContent></Card>
    </section>

    <section v-if="tab === 'users'" class="admin-section"><div class="admin-title"><div><h2>用户管理</h2><p>共 {{ matchingUsers.length }} 位用户</p></div><Input v-model="userSearch" placeholder="搜索用户名或邮箱" class="search" /></div><Card><CardContent class="pad"><div class="admin-table"><table><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>注册时间</th><th>操作</th></tr></thead><tbody><tr v-for="user in shownUsers" :key="user.id"><td><strong>{{ user.display_name || user.username }}</strong><small>{{ user.username }} · {{ user.email }}</small></td><td><StatusPill :label="user.role === 'admin' ? '管理员' : '用户'" :tone="user.role === 'admin' ? 'positive' : 'neutral'" /></td><td><StatusPill :label="user.is_active ? '启用' : '禁用'" :tone="user.is_active ? 'positive' : 'negative'" /></td><td>{{ date(user.created_at) }}</td><td><Button variant="outline" size="sm" @click="selectedUser = user">详情</Button></td></tr></tbody></table></div><p v-if="!shownUsers.length" class="admin-empty">没有匹配的用户</p><div class="pager"><Button variant="outline" size="sm" :disabled="userPage <= 1" @click="userPage--">上一页</Button>{{ userPage }} / {{ userPages }}<Button variant="outline" size="sm" :disabled="userPage >= userPages" @click="userPage++">下一页</Button></div></CardContent></Card><Card v-if="selectedUser"><CardHeader><CardTitle>用户详情 · {{ selectedUser.username }}</CardTitle></CardHeader><CardContent class="admin-form"><p class="mono">ID: {{ selectedUser.id }}</p><p>{{ selectedUser.email }}</p><div class="controls"><Button variant="outline" :disabled="lastAdmin(selectedUser)" @click="changeUser(selectedUser, { role: selectedUser.role === 'admin' ? 'user' : 'admin' })">{{ selectedUser.role === 'admin' ? '移除管理员' : '设为管理员' }}</Button><Button variant="outline" :disabled="lastAdmin(selectedUser)" @click="changeUser(selectedUser, { is_active: !selectedUser.is_active })">{{ selectedUser.is_active ? '禁用用户' : '启用用户' }}</Button></div><div class="controls"><label for="quota-adjust">额度调整</label><Input id="quota-adjust" v-model="quotaAmount" type="number" placeholder="正数增加 / 负数扣减" class="search" /><Button variant="outline" @click="adjustQuota">确认调整</Button></div><p>最近活动：{{ date(selectedUser.last_seen_at) }} · 项目：{{ selectedUser.project_count ?? 0 }} · 已用额度：{{ selectedUser.consumed_units ?? 0 }} · 已登记文件：{{ bytes(selectedUser.file_bytes) }}</p><p class="admin-muted">套餐信息尚无平台级数据。</p></CardContent></Card></section>

    <section v-if="tab === 'resources' && resources" class="admin-section"><div class="admin-title"><div><h2>资源管理</h2><p>{{ resources.scope }}</p></div></div><div class="admin-stats"><div><span>磁盘已用</span><strong>{{ bytes(resources.disk_used_bytes) }}</strong></div><div><span>磁盘可用</span><strong>{{ bytes(resources.disk_free_bytes) }}</strong></div><div><span>工作空间</span><strong>{{ resources.workspaces }}</strong></div><div><span>项目</span><strong>{{ resources.projects }}</strong></div><div><span>音乐库</span><strong>{{ resources.music_library.count }} 首 · {{ bytes(resources.music_library.size_bytes) }}</strong></div></div><div class="admin-grid"><Card><CardHeader><CardTitle>已登记文件分类</CardTitle></CardHeader><CardContent><div class="admin-table"><table><thead><tr><th>类别</th><th>文件数</th><th>大小</th></tr></thead><tbody><tr v-for="row in resources.files" :key="row.kind"><td>{{ row.kind }}</td><td>{{ row.count }}</td><td>{{ bytes(row.size_bytes) }}</td></tr></tbody></table></div><p v-if="!resources.files.length" class="admin-empty">暂无已登记文件</p></CardContent></Card><Card><CardHeader><CardTitle>用户占用前 20</CardTitle></CardHeader><CardContent><div class="admin-table"><table><thead><tr><th>用户</th><th>文件数</th><th>大小</th></tr></thead><tbody><tr v-for="row in resources.users" :key="row.username"><td>{{ row.username }}</td><td>{{ row.count }}</td><td>{{ bytes(row.size_bytes) }}</td></tr></tbody></table></div><p v-if="!resources.users.length" class="admin-empty">暂无已登记文件</p></CardContent></Card></div><p class="admin-muted">存储根目录：<span class="mono">{{ resources.root_path }}</span>。手动清理需先具备可核对的范围和恢复策略。</p></section>

    <section v-if="tab === 'settings'" class="admin-section"><div class="admin-title"><div><h2>系统配置</h2><p>平台级设置按模块管理；项目制作参数仍在工作台设置中。</p></div></div><div class="admin-grid"><Card><CardHeader><CardTitle>用户与注册</CardTitle></CardHeader><CardContent class="admin-form"><label><input v-model="registrationDraft" type="checkbox" /> 允许新用户注册</label><Button :disabled="!registration" @click="saveRegistration">保存注册设置</Button><label for="initial-quota">新用户初始额度</label><Input id="initial-quota" v-model="quotaDraft" type="number" min="0" class="search" /><Button :disabled="!quota" @click="saveQuota">保存初始额度</Button></CardContent></Card><Card><CardHeader><CardTitle>存储</CardTitle></CardHeader><CardContent class="admin-form"><label for="storage-root">统一工作空间根目录</label><Input id="storage-root" v-model="rootDraft" class="mono" /><p class="admin-muted">保存时会迁移已登记工作空间。当前来源：{{ storage?.source === 'admin' ? '管理员配置' : '部署默认值' }}</p><Button :disabled="!storage" @click="saveRoot">保存存储根目录</Button></CardContent></Card></div><Card><CardHeader><CardTitle>运行配置边界</CardTitle></CardHeader><CardContent><p class="admin-muted">LLM、TTS、音频和输出参数目前随项目配置保存。平台 Worker、重试和超时由部署环境控制。</p></CardContent></Card></section>

    <section v-if="tab === 'tasks'" class="admin-section"><div class="admin-title"><div><h2>任务 / 队列</h2><p>管理员运维视角 · 最近 {{ tasks.length }} 条</p></div><div class="controls"><select v-model="taskStatus" aria-label="任务状态" @change="load()"><option value="all">全部状态</option><option v-for="state in ['pending','queued','running','retrying','succeeded','failed','timeout','cancelled']" :key="state">{{ state }}</option></select><Input v-model="taskSearch" placeholder="任务 ID 或类型" class="search" @keyup.enter="load()" /><Button variant="outline" @click="load()">筛选</Button></div></div><Card><CardContent class="pad"><div class="admin-table"><table><thead><tr><th>类型 / ID</th><th>用户</th><th>项目</th><th>状态</th><th>Worker</th><th>创建时间</th><th>操作</th></tr></thead><tbody><tr v-for="task in tasks" :key="task.id"><td><strong>{{ task.task_type }}</strong><small class="mono">{{ task.id }}</small></td><td>{{ task.owner_username }}</td><td class="mono">{{ task.project_id?.slice(0, 8) ?? '—' }}</td><td><StatusPill :label="task.status" :tone="tone(task.status)" /></td><td class="clip" :title="task.worker_id ?? ''">{{ task.worker_id || '—' }}</td><td>{{ date(task.created_at) }}</td><td><Button variant="outline" size="sm" @click="selectedTask = task">查看</Button></td></tr></tbody></table></div><p v-if="!tasks.length" class="admin-empty">没有匹配的任务</p></CardContent></Card><Card v-if="selectedTask"><CardHeader><CardTitle>任务详情</CardTitle></CardHeader><CardContent class="admin-form"><p class="mono">{{ selectedTask.id }}</p><p>状态：{{ selectedTask.status }} · 进度 {{ selectedTask.progress }}% · 第 {{ selectedTask.attempt_no ?? 0 }} 次尝试</p><p>开始：{{ date(selectedTask.started_at) }} · 结束：{{ date(selectedTask.finished_at) }} · 耗时：{{ elapsed(selectedTask.started_at, selectedTask.finished_at) }}</p><p v-if="selectedTask.error_message" class="admin-error">{{ selectedTask.error_code }} · {{ selectedTask.error_message }}</p><Button v-if="!['succeeded','failed','timeout','cancelled'].includes(selectedTask.status)" variant="outline" @click="cancelTask(selectedTask)">取消异常任务</Button></CardContent></Card></section>

    <section v-if="tab === 'logs'" class="admin-section"><div class="admin-title"><div><h2>日志 / 异常</h2><p>任务失败、审计事件及 API 进程近五分钟 5xx；每次最多 50 条</p></div><div class="controls"><select v-model="logHours" aria-label="时间范围" @change="load()"><option :value="1">近 1 小时</option><option :value="24">近 24 小时</option><option :value="168">近 7 天</option></select><select v-model="logLevel" aria-label="日志级别" @change="load()"><option value="all">全部级别</option><option value="error">仅异常</option><option value="info">操作记录</option></select><select v-model="logModule" aria-label="日志模块" @change="load()"><option value="all">全部模块</option><option v-for="name in ['api','llm','tts','audio','bgm','worker','system']" :key="name">{{ name }}</option></select><Input v-model="logSearch" placeholder="搜索类型或消息" class="search" @keyup.enter="load()" /><Button variant="outline" @click="load()">筛选</Button></div></div><Card><CardContent class="pad"><div class="admin-table"><table><thead><tr><th>时间</th><th>级别</th><th>模块</th><th>类型</th><th>摘要</th></tr></thead><tbody><tr v-for="entry in events" :key="entry.id"><td>{{ date(entry.time) }}</td><td><StatusPill :label="entry.level" :tone="entry.level === 'error' ? 'negative' : 'neutral'" /></td><td>{{ entry.module }}</td><td>{{ entry.type }}</td><td class="clip" :title="entry.message">{{ entry.message }}</td></tr></tbody></table></div><p v-if="!events.length" class="admin-empty">没有匹配的记录</p></CardContent></Card></section>
  </div>
</template>

<style scoped>
.admin-console{min-width:0;max-width:1500px;margin:auto;padding-bottom:32px}.admin-head,.admin-title{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}.admin-head h1{font-size:26px;font-weight:750;line-height:1.25}.admin-head p,.admin-title p{color:hsl(var(--muted-foreground));font-size:13px}.admin-eyebrow{font-size:10px;font-weight:800;letter-spacing:.13em;color:hsl(var(--primary))}.admin-section{display:grid;gap:16px}.admin-title h2{font-size:19px;font-weight:750}.admin-services{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}.admin-service{min-width:0;border:1px solid hsl(var(--border));border-radius:12px;background:hsl(var(--card));padding:13px}.admin-service>div{display:flex;align-items:center;justify-content:space-between;gap:6px}.admin-service strong{font-size:13px}.admin-service p{margin-top:8px;overflow:hidden;color:hsl(var(--muted-foreground));font-size:11px;text-overflow:ellipsis;white-space:nowrap}.admin-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}.admin-stats>div{border:1px solid hsl(var(--border));border-radius:12px;background:hsl(var(--card));padding:14px}.admin-stats span{display:block;color:hsl(var(--muted-foreground));font-size:12px}.admin-stats strong{display:block;margin-top:5px;font-size:23px;font-weight:750;font-variant-numeric:tabular-nums}.admin-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.admin-grid>*{min-width:0}.admin-grid table{min-width:0}.admin-kv{display:grid;gap:10px;font-size:13px}.admin-kv p{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid hsl(var(--border));padding-bottom:8px}.admin-kv span,.admin-muted{color:hsl(var(--muted-foreground))}.admin-muted{font-size:12px;line-height:1.5}.admin-console a:not(.admin-tabs a){color:hsl(var(--primary));font-weight:650}.admin-event{display:grid;grid-template-columns:130px 1fr;gap:3px 10px;padding:8px 0;border-bottom:1px solid hsl(var(--border));font-size:11px}.admin-event span{color:hsl(var(--muted-foreground))}.admin-event p{grid-column:2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.admin-error{border:1px solid hsl(var(--destructive)/.3);border-radius:8px;background:hsl(var(--destructive)/.08);padding:10px;color:hsl(var(--destructive));font-size:12px;overflow-wrap:anywhere}.admin-error button{text-decoration:underline}.controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.controls select{min-height:36px;max-width:160px;border:1px solid hsl(var(--border));border-radius:7px;background:hsl(var(--background));padding:0 8px;font-size:13px}.search{width:215px;max-width:100%}.pad{padding-top:16px}.admin-table{width:100%;overflow-x:auto}table{width:100%;min-width:650px;border-collapse:collapse;font-size:12px}th{text-align:left;color:hsl(var(--muted-foreground));font-weight:650}th,td{padding:11px 9px;border-bottom:1px solid hsl(var(--border));vertical-align:middle}td{max-width:300px;overflow-wrap:anywhere}td small{display:block;margin-top:3px;color:hsl(var(--muted-foreground))}tr:last-child td{border-bottom:0}.clip{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.admin-empty{padding:24px;text-align:center;color:hsl(var(--muted-foreground));font-size:13px}.pager{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:12px;font-size:12px}.admin-form{display:grid;justify-items:start;gap:12px;font-size:13px}.admin-form>*{max-width:100%}.admin-form input[type=checkbox]{width:16px;height:16px;margin-right:5px;accent-color:hsl(var(--primary))}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}@media(max-width:900px){.admin-grid{grid-template-columns:1fr}}@media(max-width:600px){.admin-head h1{font-size:22px}.admin-tabs{margin:18px 0}.admin-services,.admin-stats{grid-template-columns:repeat(2,minmax(0,1fr))}.admin-title{align-items:stretch}.controls{width:100%}.controls .search{flex:1 1 150px}}
</style>
