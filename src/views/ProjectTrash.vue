<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Clock3, LoaderCircle, RotateCcw, RefreshCw, Trash2 } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import { useToast } from '@/components/ui/toast'
import { listTrashedProjects, restoreProject, type TrashedProjectSummary } from '@/api/project'
import { useProjectStore } from '@/stores/project'

const { push: toast } = useToast()
const projectStore = useProjectStore()
const projects = ref<TrashedProjectSummary[]>([])
const loading = ref(true)
const refreshing = ref(false)
const pageError = ref('')
const workingId = ref('')

function formatDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '日期未知' : date.toLocaleString()
}

function remainingDays(value: string) {
  const days = Math.ceil((Date.parse(value) - Date.now()) / 86_400_000)
  return days <= 0 ? '即将彻底删除' : `还剩 ${days} 天`
}

function isExpired(value: string) {
  return Date.parse(value) <= Date.now()
}

async function load() {
  refreshing.value = true
  pageError.value = ''
  try {
    projects.value = await listTrashedProjects()
  } catch (cause: any) {
    pageError.value = cause?.message || '暂时无法读取回收站。'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

async function restore(item: TrashedProjectSummary) {
  if (workingId.value) return
  workingId.value = item.id
  try {
    const restored = await restoreProject(item.id)
    const description = restored.name === item.name
      ? `「${item.name}」已回到项目列表。`
      : `名称冲突，已将项目恢复为「${restored.name}」。`
    toast({ title: '项目已恢复', variant: 'success', description })
    await projectStore.refresh()
    await load()
  } catch (cause: any) {
    toast({ title: '恢复失败', variant: 'destructive', description: cause?.message || '请稍后重试。' })
  } finally {
    workingId.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="project-trash">
    <header class="page-header project-trash__header">
      <div>
        <p class="eyebrow">NARRIFY AUDIO WORKSPACE</p>
        <h1 class="page-title">回收站</h1>
        <p class="page-description">移入回收站的项目会保留一个自然月，到期后由程序自动彻底删除。</p>
      </div>
      <Button variant="outline" :disabled="refreshing || !!workingId" @click="load">
        <RefreshCw class="h-4 w-4" :class="refreshing ? 'animate-spin' : ''" />刷新
      </Button>
    </header>

    <div v-if="pageError" class="trash-alert" role="alert">{{ pageError }}</div>

    <section class="trash-section" aria-labelledby="trash-heading">
      <div class="trash-heading">
        <div><h2 id="trash-heading">已删除的项目</h2><span v-if="!loading" class="muted">{{ projects.length }} 个项目</span></div>
      </div>

      <div v-if="loading" class="trash-list" aria-label="正在加载回收站">
        <Card v-for="n in 3" :key="n" class="trash-skeleton"><div class="skeleton-line w-2/5" /><div class="skeleton-line w-4/5" /></Card>
      </div>
      <div v-else-if="projects.length" class="trash-list">
        <Card v-for="item in projects" :key="item.id" class="trash-card">
          <div class="trash-card__icon"><Trash2 class="h-5 w-5" /></div>
          <div class="trash-card__copy">
            <h3>{{ item.name }}</h3>
            <p>移入时间：{{ formatDate(item.deleted_at) }}</p>
            <p class="trash-card__expiry"><Clock3 class="h-3.5 w-3.5" />{{ remainingDays(item.expires_at) }} · 到期时间：{{ formatDate(item.expires_at) }}</p>
          </div>
          <div class="trash-card__actions">
            <Button v-if="!isExpired(item.expires_at)" variant="outline" :disabled="!!workingId" @click="restore(item)">
              <LoaderCircle v-if="workingId === item.id" class="h-4 w-4 animate-spin" />
              <RotateCcw v-else class="h-4 w-4" />恢复项目
            </Button>
            <span v-else class="trash-card__expired">已到期，等待后台彻底删除</span>
          </div>
        </Card>
      </div>
      <Card v-else-if="!pageError" class="trash-empty">
        <div class="trash-empty__icon"><Trash2 class="h-6 w-6" /></div>
        <h3>回收站是空的</h3>
        <p>从项目列表删除的项目会显示在这里。</p>
      </Card>
    </section>
  </div>
</template>

<style scoped>
.project-trash{display:grid;gap:26px;max-width:1100px;margin:0 auto;padding-bottom:30px}.project-trash__header{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap}.project-trash__header::before{display:none}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.16em;color:hsl(var(--primary))}.project-trash__header h1{margin-top:5px}.project-trash__header .page-description{max-width:620px}.trash-section{display:grid;gap:12px}.trash-heading{display:flex;align-items:center;justify-content:space-between}.trash-heading>div{display:flex;align-items:baseline;gap:10px}.trash-heading h2{font-size:16px;font-weight:750}.muted,.trash-card__copy p,.trash-empty p{color:hsl(var(--muted-foreground));font-size:12px}.trash-list{display:grid;gap:12px}.trash-card{display:flex;align-items:center;gap:15px;padding:16px}.trash-card__icon,.trash-empty__icon{display:grid;place-items:center;width:42px;height:42px;flex:none;border-radius:12px;background:hsl(var(--destructive)/.08);color:hsl(var(--destructive))}.trash-card__copy{min-width:0;flex:1}.trash-card__copy h3{overflow:hidden;font-size:14px;font-weight:700;text-overflow:ellipsis;white-space:nowrap}.trash-card__copy p{margin-top:4px}.trash-card__expiry{display:flex;align-items:center;gap:5px}.trash-card__actions{display:flex;flex:none;gap:8px}.trash-card__expired{color:hsl(var(--muted-foreground));font-size:12px}.trash-empty{display:grid;justify-items:center;gap:9px;padding:44px 18px;text-align:center}.trash-empty h3{font-size:16px;font-weight:700}.trash-alert{border:1px solid hsl(var(--destructive)/.25);border-radius:10px;background:hsl(var(--destructive)/.06);padding:10px 12px;color:hsl(var(--destructive));font-size:12px}.trash-skeleton{display:grid;gap:13px;padding:18px}.skeleton-line{height:11px;border-radius:6px;background:hsl(var(--muted));animation:pulse 1.4s ease-in-out infinite}.trash-skeleton .w-2\/5{width:40%}.trash-skeleton .w-4\/5{width:80%}@keyframes pulse{50%{opacity:.4}}@media(max-width:720px){.trash-card{align-items:flex-start;flex-wrap:wrap}.trash-card__copy{flex-basis:calc(100% - 58px)}.trash-card__actions{width:100%;padding-left:57px}.trash-card__actions>*{flex:1}}@media(max-width:420px){.trash-card__actions{padding-left:0;flex-direction:column}.trash-card__actions>*{width:100%}}
</style>
