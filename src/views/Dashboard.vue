<script setup lang="ts">
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'
import { Folder, FolderOpen, Trash2 } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import StatusPill from '@/components/ui/StatusPill.vue'
import { useSettingsStore } from '@/stores/settings'
import { useWorkspaceGate } from '@/composables/useWorkspaceGate'
import { getRecentWorkspaces, getWorkspace, removeRecentWorkspace, setWorkspace, type RecentWorkspace } from '@/api/workspace'
import { useToast } from '@/components/ui/toast'
import type { WorkspaceInfo } from '@/types'

const WorkspaceBrowserDialog = defineAsyncComponent(() => import('@/components/WorkspaceBrowserDialog.vue'))

const settings = useSettingsStore()
const { workspaceSet } = useWorkspaceGate()
const { push: toast } = useToast()
const ws = ref<WorkspaceInfo | null>(null)
const recent = ref<RecentWorkspace[]>([])
const wsBusy = ref(false)
const browserOpen = ref(false)

const wsPath = computed(() => ws.value?.path || settings.config?.paths?.working_dir || '')
const wsStale = computed(() => !!ws.value?.set && ws.value.exists === false)
const wsName = computed(() => {
  const path = wsPath.value.replace(/[\\/]$/, '')
  return path.split(/[\\/]/).pop() || path
})

async function refreshWorkspaceData() {
  try {
    ws.value = await getWorkspace()
    recent.value = (await getRecentWorkspaces()).workspaces
  } catch {
    /* 后端未启动时，页面仍可显示静态状态 */
  }
}

onMounted(async () => {
  if (!settings.loaded) await settings.load()
  await refreshWorkspaceData()
})

async function applyWorkspace(path: string) {
  if (!path || wsBusy.value) return
  wsBusy.value = true
  try {
    ws.value = await setWorkspace(path)
    await settings.load()
    try { recent.value = (await getRecentWorkspaces()).workspaces } catch { /* 选择成功不因历史读取失败回滚 */ }
    browserOpen.value = false
    toast({ title: '工作空间已设置', variant: 'success', description: ws.value.path })
  } catch (e: any) {
    toast({ title: '设置工作空间失败', variant: 'destructive', description: e?.message || String(e) })
  } finally {
    wsBusy.value = false
  }
}

async function selectRecent(item: RecentWorkspace) {
  if (!item.exists) {
    toast({ title: '目录不存在', variant: 'destructive', description: '请移除这条记录，或使用“浏览文件夹”选择新的位置。' })
    return
  }
  await applyWorkspace(item.path)
}

async function removeRecent(path: string) {
  try {
    recent.value = (await removeRecentWorkspace(path)).workspaces
    toast({ title: '已从历史列表移除', description: '真实文件夹未被删除。' })
  } catch (e: any) {
    toast({ title: '移除历史记录失败', variant: 'destructive', description: e?.message || String(e) })
  }
}

async function clearWorkspace() {
  if (wsBusy.value) return
  wsBusy.value = true
  try {
    ws.value = await setWorkspace('')
    await settings.load()
    try { recent.value = (await getRecentWorkspaces()).workspaces } catch { /* 清除成功不因历史读取失败回滚 */ }
    toast({ title: '工作空间已清除', description: '流水线已重新锁定；历史记录仍保留。' })
  } catch (e: any) {
    toast({ title: '清除失败', variant: 'destructive', description: e?.message || String(e) })
  } finally {
    wsBusy.value = false
  }
}
</script>

<template>
  <div class="dashboard-page space-y-8">
    <header class="page-header">
      <h1 class="page-title">开始</h1>
      <p class="page-description">选择工作空间，开始制作有声书。</p>
    </header>

    <section class="workspace-panel p-5" :class="(!workspaceSet || wsStale) ? 'border-amber-500/60' : 'border-primary/40'">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h2 class="flex items-center gap-2 text-base font-semibold">
          <Folder class="h-5 w-5 text-primary" aria-hidden="true" />工作空间
          <StatusPill :label="!workspaceSet ? '未设置' : (wsStale ? '目录不存在' : '已设置')" :tone="!workspaceSet ? 'warning' : (wsStale ? 'negative' : 'positive')" />
        </h2>
        <Button v-if="workspaceSet" variant="outline" size="sm" :disabled="wsBusy" @click="clearWorkspace"><Trash2 class="h-4 w-4" aria-hidden="true" />清除当前</Button>
      </div>

      <div class="mt-4 rounded-xl border border-border bg-background/60 p-4">
        <div v-if="workspaceSet" class="flex flex-wrap items-center gap-3">
          <FolderOpen class="h-5 w-5 shrink-0 text-primary" aria-hidden="true" />
          <div class="min-w-0 flex-1">
            <div class="font-semibold">{{ wsName }}</div>
            <div class="break-all font-mono text-sm text-muted-foreground" :title="wsPath">{{ wsPath }}</div>
          </div>
          <StatusPill :label="wsStale ? '目录不存在' : '当前工作空间'" :tone="wsStale ? 'negative' : 'positive'" />
        </div>
        <p v-if="!workspaceSet" class="text-sm font-medium text-amber-700 dark:text-amber-400">尚未设置工作空间，流水线已锁定。请选择一个本地文件夹开始。</p>
        <p v-else-if="wsStale" class="mt-2 text-sm font-medium text-amber-700 dark:text-amber-400">工作目录已被移动或删除，请重新浏览并选择文件夹。</p>
        <p v-else class="mt-2 text-sm text-muted-foreground">所有处理结果都会保存在此目录。</p>
      </div>

      <div class="mt-4 flex flex-wrap items-center gap-3">
        <Button :disabled="wsBusy" @click="browserOpen = true"><FolderOpen class="h-4 w-4" aria-hidden="true" />浏览文件夹…</Button>
        <span class="text-xs text-muted-foreground">由后端浏览本机磁盘，不使用浏览器文件权限。</span>
      </div>

      <div class="mt-6 border-t border-border pt-5">
        <div class="mb-3 flex items-center justify-between gap-3">
          <h3 class="text-sm font-semibold">最近使用</h3>
          <span class="text-xs text-muted-foreground">点击即可切换</span>
        </div>
        <div v-if="!recent.length" class="rounded-xl border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">还没有历史工作空间。</div>
        <div v-else class="max-h-80 space-y-2 overflow-y-auto pr-1">
          <div v-for="item in recent" :key="item.path" class="flex items-center gap-3 rounded-xl border border-border px-3 py-3 transition-colors hover:bg-accent/50" :class="item.is_current ? 'border-primary/40 bg-primary/5' : ''">
            <button type="button" class="flex min-w-0 flex-1 items-center gap-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" :disabled="!item.exists || wsBusy" @click="selectRecent(item)">
              <Folder class="h-5 w-5 shrink-0 text-primary" aria-hidden="true" />
              <span class="min-w-0 flex-1">
                <span class="block truncate font-medium">{{ item.display_name }}</span>
                <span class="block break-all font-mono text-xs text-muted-foreground">{{ item.path }}</span>
              </span>
            </button>
            <StatusPill v-if="item.is_current" label="当前" tone="positive" />
            <StatusPill v-else-if="!item.exists" label="目录不存在" tone="negative" />
            <Button variant="ghost" size="icon" :aria-label="`移除历史记录 ${item.display_name}`" @click="removeRecent(item.path)"><Trash2 class="h-4 w-4" aria-hidden="true" /></Button>
          </div>
        </div>
      </div>
    </section>

    <WorkspaceBrowserDialog :open="browserOpen" @close="browserOpen = false" @select="applyWorkspace" />
  </div>
</template>
