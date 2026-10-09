<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { LogOut, Search, ShieldCheck, UserCheck, UserPlus, Users, Activity, Sparkles } from 'lucide-vue-next'
import Pager from '@/views/textformat/Pager.vue'
import AdminView from '@/components/admin/AdminView.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import AdminDrawer from '@/components/admin/AdminDrawer.vue'
import AdminEmptyState from '@/components/admin/AdminEmptyState.vue'
import AdminSegmented from '@/components/admin/AdminSegmented.vue'
import ChartCard from '@/components/admin/ChartCard.vue'
import KpiCard from '@/components/admin/KpiCard.vue'
import MeterBar from '@/components/admin/MeterBar.vue'
import AdminChart from '@/components/admin/charts/AdminChart.vue'
import { timeSeriesOption } from '@/components/admin/charts/options'
import type { ChartTokens } from '@/components/admin/charts/tokens'
import Button from '@/components/ui/Button.vue'
import Input from '@/components/ui/Input.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import { errorMessage, useAdminLoader } from '@/composables/useAdminLoader'
import * as api from '@/api/admin'
import type { ListPagination } from '@/api/listPaging'
import { bytes, compact, date, plain, relative, statusLabel, tone } from '@/utils/adminFormat'
import { series } from './series'

const { push: toast } = useToast()
const pageSize = ref(10)
const users = ref<api.AdminUser[]>([])
const usersPagination = ref<ListPagination>()
const userSearch = ref('')
const userRole = ref('all')
const userState = ref('all')
const userSort = ref('default')
const userPage = ref(1)

const loader = useAdminLoader(async (signal) => {
  const result = await api.userPage({ page: userPage.value, page_size: pageSize.value, search: userSearch.value, role: userRole.value, state: userState.value, sort: userSort.value }, signal)
  return () => { users.value = result.items; usersPagination.value = result.pagination }
}, { onActionError: message => toast({ title: '操作未完成', description: message, variant: 'destructive' }) })

watch([userSearch, userRole, userState, userSort], () => { userPage.value = 1 })
watch([userPage, userSearch, userRole, userState, userSort], () => { void loader.load() })
function changePageSize(size: number) { pageSize.value = size; if (userPage.value !== 1) userPage.value = 1; else void loader.load() }
const userFiltered = computed(() => !!userSearch.value || userRole.value !== 'all' || userState.value !== 'all')
function clearUserFilters() { userSearch.value = ''; userRole.value = 'all'; userState.value = 'all' }
const userPages = computed(() => Math.max(1, Math.ceil((usersPagination.value?.total ?? 0) / pageSize.value)))
const counts = computed(() => usersPagination.value?.counts ?? {})

function lastAdmin(user: Pick<api.AdminUser, 'role' | 'is_active'>) {
  return user.role === 'admin' && user.is_active && (usersPagination.value?.counts.active_admins ?? 0) <= 1
}
function initials(user: api.AdminUser) {
  return (user.display_name || user.username).slice(0, 1).toUpperCase()
}
function quotaTotal(user: api.AdminUser) {
  return (user.available_units ?? 0) + (user.reserved_units ?? 0) + (user.consumed_units ?? 0)
}

// ---- detail drawer
type DetailTab = 'overview' | 'usage' | 'ledger' | 'tasks'
const DETAIL_TABS = [['overview', '概览'], ['usage', '用量'], ['ledger', '额度流水'], ['tasks', '最近任务']] as const
const selectedUser = ref<api.AdminUser | null>(null)
const detailTab = ref<DetailTab>('overview')
const detail = ref<api.UserDetail | null>(null)
const ledger = ref<api.QuotaTransactionRow[]>([])
const ledgerPagination = ref<ListPagination>()
const ledgerPage = ref(1)
const ledgerPageSize = ref(10)
const quotaAmount = ref('')
let detailTicket = 0

async function openUser(user: api.AdminUser) {
  selectedUser.value = user
  detailTab.value = 'overview'
  detail.value = null
  ledger.value = []
  ledgerPage.value = 1
  await refreshDetail()
}
async function refreshDetail() {
  const user = selectedUser.value
  if (!user) return
  const ticket = ++detailTicket
  try {
    const [info, rows] = await Promise.all([api.getUserDetail(user.id), api.userQuotaTransactions(user.id, ledgerPage.value, ledgerPageSize.value)])
    if (ticket !== detailTicket) return
    detail.value = info; ledger.value = rows.items; ledgerPagination.value = rows.pagination
  } catch (cause) {
    if (ticket === detailTicket) toast({ title: '无法读取用户详情', description: errorMessage(cause), variant: 'destructive' })
  }
}
watch(ledgerPage, () => { if (selectedUser.value) void refreshDetail() })
function changeLedgerPageSize(size: number) { ledgerPageSize.value = size; if (ledgerPage.value !== 1) ledgerPage.value = 1; else if (selectedUser.value) void refreshDetail() }
watch(selectedUser, user => { if (!user) detailTicket++ })

async function changeUser(user: api.AdminUser, patch: { is_active?: boolean; role?: 'user' | 'admin' }) {
  if (!await showConfirm(`确认更改 ${user.username} 的${patch.role ? '角色' : '状态'}？`, { title: '确认修改用户' })) return
  try {
    const result = await api.updateUser(user.id, patch)
    user.role = result.role
    user.is_active = result.is_active
    toast({ title: '用户已更新', variant: 'success' })
  } catch (cause) { loader.error.value = errorMessage(cause) }
}
async function adjustQuota() {
  const user = selectedUser.value
  const amount = Number(quotaAmount.value)
  if (!user || !Number.isInteger(amount) || amount === 0) { loader.error.value = '请输入非零整数额度'; return }
  if (!await showConfirm(`确认给 ${user.username} 调整 ${amount > 0 ? '+' : ''}${amount} 单位额度？`, { title: '调整用户额度', destructive: amount < 0 })) return
  try {
    const account = await api.adjustQuota(user.id, amount, `admin-${user.id}-${Date.now()}`, '管理员后台调整')
    quotaAmount.value = ''
    user.available_units = Number(account.available_units)
    user.reserved_units = Number(account.reserved_units)
    user.consumed_units = Number(account.consumed_units)
    toast({ title: '额度已调整', variant: 'success' })
    await refreshDetail()
  } catch (cause) { loader.error.value = errorMessage(cause) }
}
async function revokeSessions() {
  const user = selectedUser.value
  if (!user) return
  if (!await showConfirm(`将吊销 ${user.username} 的全部登录会话，对方需要重新登录。继续？`, { title: '强制下线', destructive: true })) return
  try {
    const result = await api.revokeUserSessions(user.id)
    toast({ title: result.revoked ? `已吊销 ${result.revoked} 个会话` : '该用户没有其他活跃会话', variant: 'success' })
    await refreshDetail()
  } catch (cause) { loader.error.value = errorMessage(cause) }
}

const usageChart = computed(() => (t: ChartTokens) => timeSeriesOption(t, {
  scale: 'day', format: compact,
  series: [
    { name: 'TTS 字数', color: t.series[1], type: 'bar', stack: 'chars', data: series(detail.value?.daily_usage, 'tts_chars') },
    { name: 'LLM 字数', color: t.series[0], type: 'bar', stack: 'chars', data: series(detail.value?.daily_usage, 'llm_chars') },
  ],
}))
const KIND_LABELS: Record<string, string> = { consume: '额度消费', reserve: '额度预留', settle: '任务消费', release: '额度退回', admin_adjust: '管理员调整' }
/** Same sign convention as the user-facing usage page: consumption rows are deductions. */
function ledgerAmount(row: api.QuotaTransactionRow): number {
  return ['consume', 'settle'].includes(row.kind) ? -(row.char_count ?? Math.abs(row.amount)) : row.amount
}

// ---- create drawer
const creating = ref(false)
const draft = ref<api.NewUser>({ email: '', username: '', password: '', display_name: '', role: 'user' })
const createError = ref('')
function openCreate() {
  draft.value = { email: '', username: '', password: '', display_name: '', role: 'user' }
  createError.value = ''
  creating.value = true
}
async function submitCreate() {
  createError.value = ''
  const value = draft.value
  if (!/^[a-z0-9][a-z0-9._-]{5,19}$/.test(value.username)) { createError.value = '用户名必须为 6–20 位小写字母、数字、点、下划线或连字符'; return }
  if (value.password.length < 6 || value.password.length > 20) { createError.value = '密码必须为 6–20 个字符'; return }
  await loader.runAction(async () => {
    try {
      const user = await api.createUser(value)
      creating.value = false
      toast({ title: `已创建用户 ${user.username}`, variant: 'success' })
      userPage.value = 1
      await loader.loadData()
    } catch (cause) { createError.value = errorMessage(cause) }
  })
}
</script>

<template>
  <AdminView title="用户管理" description="账户、权限、额度与会话；为团队成员开户并跟踪用量。" :loader="loader">
    <template #actions>
      <Button size="sm" @click="openCreate"><UserPlus class="h-4 w-4" />新建用户</Button>
    </template>
    <div class="kpi-grid kpi-grid--5">
      <KpiCard label="用户总数" :value="plain(counts.all)" :icon="Users" :accent="0" :hint="userFiltered ? '当前筛选结果' : '全部账户'" />
      <KpiCard label="管理员" :value="plain(counts.admins)" :icon="ShieldCheck" :accent="6" :hint="`${plain(counts.active_admins)} 位启用`" />
      <KpiCard label="已启用" :value="plain(counts.enabled)" :icon="UserCheck" :accent="2" />
      <KpiCard label="近 7 天新增" :value="plain(counts.new_7d)" :icon="Sparkles" :accent="1" />
      <KpiCard label="15 分钟内活跃" :value="plain(counts.active_15m)" :icon="Activity" :accent="3" />
    </div>

    <section class="table-panel">
      <div class="table-toolbar">
        <div class="controls">
          <div class="search-field"><Search class="h-4 w-4" aria-hidden="true" /><Input v-model="userSearch" aria-label="搜索用户" placeholder="搜索用户名、昵称或邮箱" class="search" /></div>
          <select v-model="userRole" aria-label="用户角色"><option value="all">全部角色</option><option value="admin">管理员</option><option value="user">普通用户</option></select>
          <select v-model="userState" aria-label="账户状态"><option value="all">全部状态</option><option value="active">已启用</option><option value="disabled">已禁用</option></select>
          <select v-model="userSort" aria-label="用户排序"><option value="default">最近注册</option><option value="name">用户名</option><option value="storage">存储占用最多</option></select>
          <Button v-if="userFiltered" variant="ghost" size="sm" @click="clearUserFilters">清空筛选</Button>
        </div>
      </div>
      <AdminTable table-class="wide-table users-table" :page-size="pageSize">
        <thead><tr><th>用户</th><th>角色 / 状态</th><th>注册 / 最近活跃</th><th>额度使用</th><th>项目</th><th>已登记存储</th><th class="user-actions">操作</th></tr></thead>
        <tbody>
          <tr v-for="user in users" :key="user.id" :aria-selected="selectedUser?.id === user.id">
            <td><div class="user-cell"><span class="avatar" aria-hidden="true">{{ initials(user) }}</span><div><strong>{{ user.display_name || user.username }}</strong><small>{{ user.username }} · {{ user.email }}</small></div></div></td>
            <td><div class="badge-stack"><StatusPill :label="user.role === 'admin' ? '管理员' : '用户'" :tone="user.role === 'admin' ? 'positive' : 'neutral'" /><StatusPill :label="user.is_active ? '启用' : '禁用'" :tone="user.is_active ? 'positive' : 'negative'" /></div></td>
            <td>{{ date(user.created_at) }}<small>最近活跃 {{ relative(user.last_seen_at) }}</small></td>
            <td class="meter-cell"><MeterBar :value="user.consumed_units ?? 0" :max="quotaTotal(user)" :label="`${compact(user.consumed_units ?? 0)} / ${compact(quotaTotal(user))}`" :warn=".8" :critical=".95" /><small>{{ compact(user.available_units ?? 0) }} 可用 · {{ compact(user.reserved_units ?? 0) }} 预留</small></td>
            <td>{{ user.project_count ?? 0 }} 个</td>
            <td>{{ bytes(user.storage_bytes) }}<small>{{ user.file_count ?? 0 }} 个文件</small></td>
            <td class="user-actions"><Button variant="outline" size="sm" @click="openUser(user)">管理</Button></td>
          </tr>
          <tr v-if="!users.length"><td colspan="7" class="admin-empty-cell">没有匹配的用户，尝试其他用户名或邮箱，或清空搜索条件。<Button v-if="userFiltered" variant="outline" size="sm" class="ml-2" @click="clearUserFilters">清空搜索</Button></td></tr>
        </tbody>
      </AdminTable>
      <Pager :page="userPage" :page-count="userPages" :total="usersPagination?.total ?? 0" :page-size="pageSize" unit="位用户" @update:page="userPage = $event" @update:page-size="changePageSize" />
    </section>

    <AdminDrawer v-if="selectedUser" :title="selectedUser.display_name || selectedUser.username" @close="selectedUser = null">
      <div class="drawer-stack">
        <div class="drawer-hero">
          <div class="user-cell"><span class="avatar avatar--lg" aria-hidden="true">{{ initials(selectedUser) }}</span><div><strong>{{ selectedUser.username }}</strong><p>{{ selectedUser.email }}</p></div></div>
          <div class="badge-stack"><StatusPill :label="selectedUser.role === 'admin' ? '管理员' : '用户'" :tone="selectedUser.role === 'admin' ? 'positive' : 'neutral'" /><StatusPill :label="selectedUser.is_active ? '启用' : '禁用'" :tone="selectedUser.is_active ? 'positive' : 'negative'" /></div>
        </div>
        <p v-if="loader.error.value" role="alert" class="admin-error">{{ loader.error.value }}</p>
        <AdminSegmented v-model="detailTab" :options="DETAIL_TABS" label="用户详情分区" />

        <template v-if="detailTab === 'overview'">
          <dl class="detail-grid">
            <div><dt>注册时间</dt><dd>{{ date(selectedUser.created_at) }}</dd></div>
            <div><dt>最近活跃</dt><dd>{{ relative(detail?.sessions.last_seen_at ?? selectedUser.last_seen_at) }}</dd></div>
            <div><dt>活跃会话</dt><dd>{{ detail ? detail.sessions.active : '—' }}</dd></div>
            <div><dt>项目</dt><dd>{{ selectedUser.project_count ?? 0 }} 个 · {{ bytes(selectedUser.storage_bytes) }}</dd></div>
          </dl>
          <div class="quota-summary">
            <div class="quota-summary__head"><strong>制作额度</strong><span>{{ compact(quotaTotal(selectedUser)) }} 单位</span></div>
            <div class="stacked-meter" role="img" :aria-label="`已用 ${selectedUser.consumed_units ?? 0}，预留 ${selectedUser.reserved_units ?? 0}，可用 ${selectedUser.available_units ?? 0}`">
              <span class="is-used" :style="{ flexGrow: selectedUser.consumed_units ?? 0 }" /><span class="is-reserved" :style="{ flexGrow: selectedUser.reserved_units ?? 0 }" /><span class="is-free" :style="{ flexGrow: selectedUser.available_units ?? 0 }" />
            </div>
            <ul class="legend-row"><li><i class="is-used" />已用 {{ compact(selectedUser.consumed_units ?? 0) }}</li><li><i class="is-reserved" />预留 {{ compact(selectedUser.reserved_units ?? 0) }}</li><li><i class="is-free" />可用 {{ compact(selectedUser.available_units ?? 0) }}</li></ul>
          </div>
          <div class="admin-setting-row">
            <div><label for="quota-adjust">额度调整</label><p>正数增加、负数扣减，写入额度流水。</p></div>
            <div class="controls"><Input id="quota-adjust" v-model="quotaAmount" type="number" placeholder="如 1000 或 -200" class="w-36" /><Button :disabled="loader.actionBusy.value" @click="loader.runAction(adjustQuota)">确认调整</Button></div>
          </div>
          <div class="admin-setting-row">
            <div><h3>账户权限</h3><p v-if="lastAdmin(selectedUser)">此账户是最后一位启用的管理员，无法移除权限或禁用。</p><p v-else>切换管理员角色或停用账户（停用后无法登录）。</p></div>
            <div class="controls">
              <Button variant="outline" size="sm" :disabled="loader.actionBusy.value || lastAdmin(selectedUser)" @click="loader.runAction(async () => { if (selectedUser) await changeUser(selectedUser, { role: selectedUser.role === 'admin' ? 'user' : 'admin' }) })">{{ selectedUser.role === 'admin' ? '移除管理员' : '设为管理员' }}</Button>
              <Button variant="outline" size="sm" :disabled="loader.actionBusy.value || lastAdmin(selectedUser)" @click="loader.runAction(async () => { if (selectedUser) await changeUser(selectedUser, { is_active: !selectedUser.is_active }) })">{{ selectedUser.is_active ? '禁用用户' : '启用用户' }}</Button>
            </div>
          </div>
          <div class="admin-setting-row">
            <div><h3>强制下线</h3><p>吊销该用户全部登录会话（你自己当前的会话除外）。</p></div>
            <div class="controls"><Button variant="destructive" size="sm" :disabled="loader.actionBusy.value || !detail?.sessions.active" @click="loader.runAction(revokeSessions)"><LogOut class="h-4 w-4" />吊销 {{ detail?.sessions.active ?? 0 }} 个会话</Button></div>
          </div>
        </template>

        <template v-else-if="detailTab === 'usage'">
          <ChartCard title="近 30 天每日用量" subtitle="TTS 输入字数与 LLM 输出字数（额度账本实耗）" :span="12"
                     :columns="[{ key: 'time', label: '日期', format: v => new Date(v).toLocaleDateString('zh-CN') }, { key: 'tts_chars', label: 'TTS 字数' }, { key: 'llm_chars', label: 'LLM 字数' }, { key: 'tasks', label: '提交任务' }]" :rows="detail?.daily_usage ?? []">
            <AdminChart :option="usageChart" :height="240" label="用户近 30 天每日用量柱状图" />
          </ChartCard>
          <dl v-if="detail" class="detail-grid">
            <div v-for="(value, status) in detail.task_statuses" :key="status"><dt>{{ statusLabel(String(status)) }}</dt><dd>{{ plain(value) }} 个任务</dd></div>
          </dl>
        </template>

        <template v-else-if="detailTab === 'ledger'">
          <AdminTable table-class="compact-table ledger-table" :page-size="ledgerPageSize">
            <thead><tr><th>时间</th><th>类型</th><th>变动</th><th>资源</th><th>结余</th><th>备注</th></tr></thead>
            <tbody><tr v-for="row in ledger" :key="row.id">
              <td>{{ date(row.time) }}</td><td>{{ KIND_LABELS[row.kind] ?? row.kind }}</td>
              <td :class="ledgerAmount(row) < 0 ? 'text-danger' : ''">{{ ledgerAmount(row) > 0 ? '+' : '' }}{{ plain(ledgerAmount(row)) }}</td>
              <td>{{ row.resource_type ?? '—' }}<small v-if="row.operation_type">{{ row.operation_type }}</small></td>
              <td>{{ row.available_after == null ? '—' : plain(row.available_after) }}</td>
              <td class="clip" :title="row.note">{{ row.note || '—' }}</td>
            </tr><tr v-if="!ledger.length"><td colspan="6" class="admin-empty-cell">暂无额度流水</td></tr></tbody>
          </AdminTable>
          <Pager :page="ledgerPage" :page-count="Math.max(1, Math.ceil((ledgerPagination?.total ?? 0) / ledgerPageSize))" :total="ledgerPagination?.total ?? 0" :page-size="ledgerPageSize" unit="条" @update:page="ledgerPage = $event" @update:page-size="changeLedgerPageSize" />
        </template>

        <template v-else>
          <ul v-if="detail?.recent_tasks.length" class="event-list">
            <li v-for="task in detail.recent_tasks" :key="task.id">
              <span class="status-dot" :class="`is-${tone(task.status)}`" aria-hidden="true" />
              <div><strong>{{ task.task_type }}</strong><p class="mono">{{ task.id }}</p></div>
              <span class="event-list__meta"><StatusPill :label="statusLabel(task.status)" :tone="tone(task.status)" /><small>{{ relative(task.created_at) }}</small></span>
            </li>
          </ul>
          <AdminEmptyState v-else title="暂无任务" />
        </template>
      </div>
    </AdminDrawer>

    <AdminDrawer v-if="creating" title="新建用户" @close="creating = false">
      <form class="admin-form" @submit.prevent="submitCreate">
        <p class="admin-muted">无论是否开放自助注册，管理员都可以直接开户；新账户获得默认工作空间与初始额度。</p>
        <label for="new-email">邮箱</label><Input id="new-email" v-model="draft.email" type="email" required autocomplete="off" />
        <label for="new-username">用户名</label><Input id="new-username" v-model="draft.username" required minlength="6" maxlength="20" autocomplete="off" placeholder="6–20 位小写字母、数字、. _ -" />
        <label for="new-display">昵称（可选）</label><Input id="new-display" v-model="draft.display_name" maxlength="120" autocomplete="off" />
        <label for="new-password">初始密码</label><Input id="new-password" v-model="draft.password" type="password" required minlength="6" maxlength="20" autocomplete="new-password" />
        <label for="new-role">角色</label>
        <select id="new-role" v-model="draft.role"><option value="user">普通用户</option><option value="admin">管理员</option></select>
        <p v-if="createError" role="alert" class="admin-error">{{ createError }}</p>
        <div class="controls"><Button type="submit" :disabled="loader.actionBusy.value"><UserPlus class="h-4 w-4" />{{ loader.actionBusy.value ? '创建中…' : '创建用户' }}</Button><Button type="button" variant="ghost" @click="creating = false">取消</Button></div>
      </form>
    </AdminDrawer>
  </AdminView>
</template>
