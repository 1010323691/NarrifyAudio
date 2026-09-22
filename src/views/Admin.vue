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
const quotaDraft = ref<Record<string, string>>({})
const busy = ref(false)

async function load() {
  busy.value = true
  try {
    const [userRows, storageSettings] = await Promise.all([adminApi.listUsers(), adminApi.getStorageSettings()])
    users.value = userRows
    storage.value = storageSettings
    rootPath.value = storageSettings.root_path
  } catch (error: any) {
    toast({ title: '管理数据加载失败', description: error?.message || String(error), variant: 'destructive' })
  } finally {
    busy.value = false
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
      <CardHeader><CardTitle>用户管理</CardTitle></CardHeader>
      <CardContent>
        <div class="overflow-x-auto">
          <table class="w-full min-w-[48rem] text-sm">
            <thead><tr class="border-b text-left text-muted-foreground"><th class="p-3">用户</th><th class="p-3">角色</th><th class="p-3">状态</th><th class="p-3">额度调整</th><th class="p-3">操作</th></tr></thead>
            <tbody>
              <tr v-for="user in users" :key="user.id" class="border-b last:border-0">
                <td class="p-3"><div class="font-medium">{{ user.display_name }}</div><div class="text-xs text-muted-foreground">{{ user.username }} · {{ user.email }}</div></td>
                <td class="p-3"><StatusPill :label="user.role === 'admin' ? '管理员' : '用户'" :tone="user.role === 'admin' ? 'positive' : 'neutral'" /></td>
                <td class="p-3"><StatusPill :label="user.is_active ? '启用' : '禁用'" :tone="user.is_active ? 'positive' : 'negative'" /></td>
                <td class="p-3"><div class="flex gap-2"><Input v-model="quotaDraft[user.id]" class="w-28" type="number" placeholder="+/- 单位" /><Button variant="outline" size="sm" @click="adjust(user)">调整</Button></div></td>
                <td class="p-3"><Button variant="ghost" size="sm" :disabled="user.role === 'admin' && user.is_active" @click="toggleUser(user)"><UserX v-if="user.is_active" class="h-4 w-4" /><UserCheck v-else class="h-4 w-4" />{{ user.is_active ? '禁用' : '启用' }}</Button></td>
              </tr>
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  </div>
</template>
