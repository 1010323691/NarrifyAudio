<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RefreshCw, Save, ShieldCheck, UserCheck, UserX } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import CardContent from '@/components/ui/CardContent.vue'
import CardDescription from '@/components/ui/CardDescription.vue'
import CardHeader from '@/components/ui/CardHeader.vue'
import CardTitle from '@/components/ui/CardTitle.vue'
import Input from '@/components/ui/Input.vue'
import Label from '@/components/ui/Label.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useToast } from '@/components/ui/toast'
import * as adminApi from '@/api/admin'

const { push: toast } = useToast()
const users = ref<adminApi.AdminUser[]>([])
const storage = ref<adminApi.StorageSettings | null>(null)
const rootPath = ref('')
const initialQuota = ref('0')
const quotaSettings = ref<adminApi.QuotaSettings | null>(null)
const registrationSettings = ref<adminApi.RegistrationSettings | null>(null)
const registrationEnabled = ref(true)
const quotaDraft = ref<Record<string, string>>({})
const tasks = ref<adminApi.AdminTask[]>([])
const workers = ref<adminApi.WorkerStatus[]>([])
const queue = ref<adminApi.QueueStatus | null>(null)
const busy = ref(false)

function isLastActiveAdmin(user: adminApi.AdminUser): boolean {
  return user.role === 'admin' && user.is_active && users.value.filter(item => item.role === 'admin' && item.is_active).length <= 1
}

async function load() {
  busy.value = true
  try {
    const [userRows, storageSettings, quota, registration, taskRows, workerRows, queueStatus] = await Promise.all([
      adminApi.listUsers(), adminApi.getStorageSettings(), adminApi.getQuotaSettings(), adminApi.getRegistrationSettings(), adminApi.listTasks(), adminApi.listWorkers(), adminApi.getQueueStatus(),
    ])
    users.value = userRows
    storage.value = storageSettings
    rootPath.value = storageSettings.root_path
    quotaSettings.value = quota
    initialQuota.value = String(quota.initial_units)
    registrationSettings.value = registration
    registrationEnabled.value = registration.enabled
    tasks.value = taskRows
    workers.value = workerRows
    queue.value = queueStatus
  } catch (error: any) {
    toast({ title: '管理数据加载失败', description: error?.message || String(error), variant: 'destructive' })
  } finally {
    busy.value = false
  }
}

async function saveRegistration() {
  try {
    registrationSettings.value = await adminApi.updateRegistrationSettings(registrationEnabled.value)
    toast({ title: registrationEnabled.value ? '注册已开启' : '注册已关闭', variant: 'success' })
  } catch (error: any) {
    toast({ title: '保存注册设置失败', description: error?.message || String(error), variant: 'destructive' })
  }
}

async function saveInitialQuota() {
  const units = Number(initialQuota.value)
  if (!Number.isInteger(units) || units < 0) return
  try {
    quotaSettings.value = await adminApi.updateQuotaSettings(units)
    toast({ title: '初始额度已保存', variant: 'success' })
  } catch (error: any) {
    toast({ title: '保存初始额度失败', description: error?.message || String(error), variant: 'destructive' })
  }
}

async function saveRoot() {
  try {
    storage.value = await adminApi.updateStorageRoot(rootPath.value)
    toast({ title: '存储根目录已保存', variant: 'success' })
  } catch (error: any) {
    toast({ title: '保存失败', description: error?.message || String(error), variant: 'destructive' })
  }
}

async function toggleUser(user: adminApi.AdminUser) {
  try {
    const updated = await adminApi.updateUser(user.id, { is_active: !user.is_active })
    user.is_active = updated.is_active
    toast({ title: user.is_active ? '用户已启用' : '用户已禁用', variant: 'success' })
  } catch (error: any) {
    toast({ title: '更新用户失败', description: error?.message || String(error), variant: 'destructive' })
  }
}

async function toggleRole(user: adminApi.AdminUser) {
  try {
    const updated = await adminApi.updateUser(user.id, { role: user.role === 'admin' ? 'user' : 'admin' })
    user.role = updated.role
    toast({ title: user.role === 'admin' ? '已授予管理员角色' : '已移除管理员角色', variant: 'success' })
  } catch (error: any) {
    toast({ title: '更新角色失败', description: error?.message || String(error), variant: 'destructive' })
  }
}

async function adjust(user: adminApi.AdminUser) {
  const amount = Number(quotaDraft.value[user.id] || 0)
  if (!Number.isInteger(amount) || amount === 0) return
  try {
    await adminApi.adjustQuota(user.id, amount, `admin-${user.id}-${Date.now()}`, '管理员后台调整')
    quotaDraft.value[user.id] = ''
    toast({ title: '额度已调整', variant: 'success' })
  } catch (error: any) {
    toast({ title: '额度调整失败', description: error?.message || String(error), variant: 'destructive' })
  }
}

async function cancelTask(task: adminApi.AdminTask) {
  try {
    const updated = await adminApi.cancelTask(task.id)
    task.status = updated.status
    toast({ title: '任务已取消', variant: 'success' })
  } catch (error: any) {
    toast({ title: '取消任务失败', description: error?.message || String(error), variant: 'destructive' })
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-6">
    <header class="page-header flex items-start justify-between gap-4">
      <div>
        <h1 class="page-title flex items-center gap-2"><ShieldCheck class="h-6 w-6 text-primary" />管理后台</h1>
        <p class="page-description">管理用户状态、额度和统一工作空间存储根目录。</p>
      </div>
      <Button variant="outline" :disabled="busy" @click="load"><RefreshCw class="h-4 w-4" />刷新</Button>
    </header>

    <Card>
      <CardHeader>
        <CardTitle>统一存储根目录</CardTitle>
        <CardDescription>所有平台工作空间将按「根目录 / 用户名 / 工作空间或项目 ID」落盘。</CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <Label for="storage-root">绝对路径</Label>
        <div class="flex flex-wrap gap-2">
          <Input id="storage-root" v-model="rootPath" class="min-w-[20rem] flex-1 font-mono" />
          <Button @click="saveRoot"><Save class="h-4 w-4" />保存</Button>
        </div>
        <p v-if="storage" class="text-xs text-muted-foreground">来源：{{ storage.source === 'admin' ? '管理员设置' : '部署默认值' }}</p>
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle>用户注册</CardTitle><CardDescription>关闭后阻止新用户注册，不影响已有用户登录。</CardDescription></CardHeader>
      <CardContent class="flex flex-wrap items-center gap-3">
        <label class="flex items-center gap-2 text-sm"><input v-model="registrationEnabled" type="checkbox" class="h-4 w-4 accent-primary" />允许新用户注册</label>
        <Button @click="saveRegistration"><Save class="h-4 w-4" />保存</Button>
        <span v-if="registrationSettings" class="text-xs text-muted-foreground">来源：{{ registrationSettings.source === 'admin' ? '管理员设置' : '部署默认值' }}</span>
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle>新用户初始额度</CardTitle><CardDescription>仅影响之后注册的用户，默认 0；已有用户额度不会被覆盖。</CardDescription></CardHeader>
      <CardContent class="flex flex-wrap items-end gap-3">
        <div><Label for="initial-quota">额度单位</Label><Input id="initial-quota" v-model="initialQuota" class="mt-1 w-40" type="number" min="0" step="1" /></div>
        <Button @click="saveInitialQuota"><Save class="h-4 w-4" />保存</Button>
        <span v-if="quotaSettings" class="text-xs text-muted-foreground">来源：{{ quotaSettings.source === 'admin' ? '管理员设置' : '部署默认值' }}</span>
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle>用户管理</CardTitle></CardHeader>
      <CardContent>
        <div class="overflow-x-auto">
          <table class="w-full min-w-[48rem] text-sm">
            <thead><tr class="border-b text-left text-muted-foreground"><th class="p-3">用户</th><th class="p-3">角色</th><th class="p-3">状态</th><th class="p-3">额度调整</th><th class="p-3">操作</th></tr></thead>
            <tbody>
              <tr v-for="user in users" :key="user.id" class="border-b last:border-0">
                <td class="p-3"><div class="font-medium">{{ user.display_name }}</div><div class="text-xs text-muted-foreground">{{ user.username }} · {{ user.email }}</div></td>
                <td class="p-3"><div class="flex items-center gap-2"><StatusPill :label="user.role === 'admin' ? '管理员' : '用户'" :tone="user.role === 'admin' ? 'positive' : 'neutral'" /><Button variant="ghost" size="sm" :disabled="isLastActiveAdmin(user)" @click="toggleRole(user)">{{ user.role === 'admin' ? '降为用户' : '设为管理员' }}</Button></div></td>
                <td class="p-3"><StatusPill :label="user.is_active ? '启用' : '禁用'" :tone="user.is_active ? 'positive' : 'negative'" /></td>
                <td class="p-3"><div class="flex gap-2"><Input v-model="quotaDraft[user.id]" class="w-28" type="number" placeholder="+/- 单位" /><Button variant="outline" size="sm" @click="adjust(user)">调整</Button></div></td>
                <td class="p-3"><Button variant="ghost" size="sm" :disabled="isLastActiveAdmin(user)" @click="toggleUser(user)"><UserX v-if="user.is_active" class="h-4 w-4" /><UserCheck v-else class="h-4 w-4" />{{ user.is_active ? '禁用' : '启用' }}</Button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>

    <div class="grid gap-6 xl:grid-cols-2">
      <Card>
        <CardHeader><CardTitle>Worker 状态</CardTitle><CardDescription>以数据库心跳判断在线状态。</CardDescription></CardHeader>
        <CardContent>
          <div v-if="!workers.length" class="text-sm text-muted-foreground">暂无 Worker 心跳。</div>
          <div v-for="worker in workers" :key="worker.worker_id" class="flex items-center justify-between border-b py-2 last:border-0">
            <div><div class="font-medium">{{ worker.worker_id }}</div><div class="text-xs text-muted-foreground">{{ worker.last_seen_at }}</div></div>
            <StatusPill :label="worker.status" :tone="worker.status === 'idle' ? 'positive' : worker.status === 'offline' ? 'negative' : 'neutral'" />
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Redis Streams 队列</CardTitle><CardDescription>队列长度与消费者待确认消息。</CardDescription></CardHeader>
        <CardContent>
          <div v-if="queue" class="grid grid-cols-3 gap-3 text-center">
            <div class="rounded-lg border p-3"><div class="text-2xl font-semibold">{{ queue.length }}</div><div class="text-xs text-muted-foreground">消息</div></div>
            <div class="rounded-lg border p-3"><div class="text-2xl font-semibold">{{ queue.pending }}</div><div class="text-xs text-muted-foreground">待确认</div></div>
            <div class="rounded-lg border p-3"><div class="text-sm font-semibold">{{ queue.available ? '正常' : '不可用' }}</div><div class="text-xs text-muted-foreground">连接状态</div></div>
          </div>
        </CardContent>
      </Card>
    </div>

    <Card>
      <CardHeader><CardTitle>全局任务</CardTitle><CardDescription>最近 500 条持久化任务。</CardDescription></CardHeader>
      <CardContent>
        <div class="overflow-x-auto">
          <table class="w-full min-w-[50rem] text-sm">
            <thead><tr class="border-b text-left text-muted-foreground"><th class="p-3">任务</th><th class="p-3">用户</th><th class="p-3">状态</th><th class="p-3">进度</th><th class="p-3">创建时间</th><th class="p-3">操作</th></tr></thead>
            <tbody><tr v-for="task in tasks" :key="task.id" class="border-b last:border-0"><td class="p-3"><div class="font-medium">{{ task.task_type }}</div><div class="text-xs text-muted-foreground">{{ task.id }}</div></td><td class="p-3">{{ task.owner_username }}</td><td class="p-3"><StatusPill :label="task.status" :tone="task.status === 'succeeded' ? 'positive' : ['failed', 'timeout'].includes(task.status) ? 'negative' : 'neutral'" /></td><td class="p-3">{{ task.progress }}%</td><td class="p-3 text-xs text-muted-foreground">{{ task.created_at }}</td><td class="p-3"><Button v-if="!['succeeded', 'failed', 'cancelled', 'timeout'].includes(task.status)" variant="ghost" size="sm" @click="cancelTask(task)">取消</Button></td></tr></tbody>
          </table>
          <div v-if="!tasks.length" class="py-6 text-center text-sm text-muted-foreground">暂无持久化任务。</div>
        </div>
      </CardContent>
    </Card>
  </div>
</template>
