<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { LoaderCircle, RotateCcw, RefreshCw, Search, Trash2 } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import { useToast } from '@/components/ui/toast'
import { listProjectPage, restoreProject, type TrashedProjectSummary } from '@/api/project'
import { useListPage } from '@/composables/useListPage'
import Pager from '@/views/textformat/Pager.vue'
import { useProjectStore } from '@/stores/project'

const { push: toast } = useToast()
const projectStore = useProjectStore()
const projects = ref<TrashedProjectSummary[]>([])
const loading = ref(true)
const refreshing = ref(false)
const pageError = ref('')
const workingId = ref('')
const search = ref('')
const expiryFilter = ref<'all' | 'active' | 'expired'>('all')
const listPage = useListPage(load, () => { projects.value = [] })
const pagination = listPage.pagination
const page = ref(1)
const filteredProjects = computed(() => projects.value)
const activeCount = computed(() => pagination.value?.counts.active ?? 0)
watch([search, expiryFilter], () => { page.value = 1; void load() })
watch(page, () => { void load() })

function formatDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '日期未知' : date.toLocaleString()
}

function remainingDays(value: string) {
  const days = Math.ceil((Date.parse(value) - Date.now()) / 86_400_000)
  return days <= 0 ? '即将彻底删除' : `还剩 ${days} 天`
}

function remainingCount(value: string) {
  return Math.max(0, Math.ceil((Date.parse(value) - Date.now()) / 86_400_000))
}

function retentionPercent(item: TrashedProjectSummary) {
  const deleted = Date.parse(item.deleted_at)
  const expires = Date.parse(item.expires_at)
  const now = Date.now()
  if (!Number.isFinite(deleted) || !Number.isFinite(expires) || expires <= deleted) return 0
  return Math.max(0, Math.min(100, ((expires - now) / (expires - deleted)) * 100))
}

function isExpired(value: string) {
  return Date.parse(value) <= Date.now()
}

async function load() {
  refreshing.value = true
  pageError.value = ''
  const signal = listPage.begin()
  try {
    const response = await listProjectPage({ page: page.value, page_size: 12, q: search.value, filter: expiryFilter.value }, signal, true)
    if (signal.aborted) return
    pageError.value = ''
    projects.value = response.items; listPage.received(response.pagination, page)
  } catch (cause: any) {
    if (!signal.aborted) pageError.value = cause?.message || '暂时无法读取回收站。'
  } finally {
    if (!signal.aborted) { loading.value = false; refreshing.value = false }
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
  <div class="project-trash viewport-page">
    <WorkbenchContextBar>
      <template #icon><Trash2 /></template>
      <template #title>回收站</template>
      <template #description>项目保留一个自然月；到期后由程序自动彻底删除。</template>
      <template #metrics>
        <div class="workbench-context-metric"><strong>{{ loading ? '…' : (pagination?.counts.all ?? projects.length) }}</strong>已删除项目</div>
        <div class="workbench-context-metric"><strong>{{ loading ? '…' : activeCount }}</strong>可恢复</div>
      </template>
      <template #actions>
        <Button variant="outline" :disabled="refreshing || !!workingId" @click="load">
          <RefreshCw class="h-4 w-4" :class="refreshing ? 'animate-spin' : ''" />{{ refreshing ? '刷新中' : '刷新' }}
        </Button>
      </template>
    </WorkbenchContextBar>

    <div class="page-region" role="region" aria-label="页面工作区" tabindex="0">
      <div v-if="pageError" class="trash-alert" role="alert">{{ pageError }}</div>

      <section class="trash-section" aria-labelledby="trash-heading">
        <div class="trash-heading">
          <div><h2 id="trash-heading">已删除的项目</h2><span v-if="!loading" class="muted">按最早到期时间排列</span></div>
          <div class="trash-filters">
            <label class="trash-search"><Search class="h-4 w-4" /><input v-model="search" aria-label="搜索回收站项目" placeholder="搜索项目名称" /></label>
            <select v-model="expiryFilter" aria-label="筛选项目保留状态"><option value="all">全部状态</option><option value="active">可恢复</option><option value="expired">已到期</option></select>
          </div>
        </div>

        <div v-if="loading" class="trash-table" aria-label="正在加载回收站">
          <div class="trash-table__head"><span>项目</span><span>移入时间</span><span>保留期限</span><span>操作</span></div>
          <div v-for="n in 4" :key="n" class="trash-skeleton"><div class="skeleton-line w-2/5" /><div class="skeleton-line w-4/5" /></div>
        </div>
        <div v-else-if="filteredProjects.length" class="trash-table" role="table" aria-label="回收站项目">
          <div class="trash-table__head" role="row">
            <span role="columnheader">项目</span><span role="columnheader">移入时间</span><span role="columnheader">保留期限</span><span role="columnheader">操作</span>
          </div>
          <div v-for="item in filteredProjects" :key="item.id" class="trash-row" role="row">
            <div class="trash-row__project" role="cell">
              <div class="trash-card__icon"><Trash2 class="h-4 w-4" /></div>
              <div class="trash-card__copy"><h3 :title="item.name">{{ item.name }}</h3><p>项目已移至回收站</p></div>
            </div>
            <div class="trash-row__date" role="cell"><span>移入时间</span><time>{{ formatDate(item.deleted_at) }}</time></div>
            <div class="trash-row__retention" role="cell">
              <div class="trash-row__retention-label" :class="{ 'is-urgent': remainingCount(item.expires_at) <= 7 }">
                <span class="trash-retention-pill"><i />{{ isExpired(item.expires_at) ? '等待清理' : '可恢复' }}</span>
                <strong>{{ remainingDays(item.expires_at) }}</strong>
              </div>
              <div class="trash-retention-track" :class="{ 'is-urgent': remainingCount(item.expires_at) <= 7 }"><span :style="{ width: `${retentionPercent(item)}%` }" /></div>
              <small>自动清理：{{ formatDate(item.expires_at) }}</small>
            </div>
            <div class="trash-card__actions" role="cell">
              <Button v-if="!isExpired(item.expires_at)" variant="outline" :disabled="!!workingId" @click="restore(item)">
                <LoaderCircle v-if="workingId === item.id" class="h-4 w-4 animate-spin" />
                <RotateCcw v-else class="h-4 w-4" />{{ workingId === item.id ? '恢复中…' : '恢复项目' }}
              </Button>
              <span v-else class="trash-card__expired">到期后自动删除</span>
            </div>
          </div>
        </div>
        <Card v-else-if="!pageError" class="trash-empty">
          <div class="trash-empty__icon"><Trash2 class="h-6 w-6" /></div>
          <h3>{{ projects.length ? '没有匹配的项目' : '回收站是空的' }}</h3>
          <p>{{ projects.length ? '调整搜索条件或状态筛选。' : '从项目列表删除的项目会显示在这里。' }}</p>
        </Card>
      </section>
      <Pager :page="page" :page-count="Math.max(1, Math.ceil((pagination?.total ?? 0) / 12))" :total="pagination?.total ?? 0" :page-size="12" unit="个项目" @update:page="page = $event" />
    </div>
  </div>
</template>

<style scoped>
.project-trash{gap:10px}.trash-section{display:grid;gap:12px;min-height:0}.trash-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.trash-heading>div:first-child{display:flex;align-items:baseline;gap:10px}.trash-heading h2{font-size:14px;font-weight:750}.muted,.trash-card__copy p,.trash-empty p{color:hsl(var(--muted-foreground));font-size:12px}.trash-filters{display:flex;align-items:center;gap:8px}.trash-search{display:flex;align-items:center;gap:8px;width:min(280px,32vw);height:34px;padding:0 10px;border:1px solid hsl(var(--input));border-radius:9px;color:hsl(var(--muted-foreground));background:hsl(var(--card))}.trash-search input{min-width:0;width:100%;border:0;outline:0;background:transparent;color:hsl(var(--foreground));font-size:12px}.trash-filters select{height:34px;padding:0 10px;border:1px solid hsl(var(--input));border-radius:9px;background:hsl(var(--card));font-size:12px}.trash-table{overflow:hidden;border:1px solid hsl(var(--border));border-radius:13px;background:hsl(var(--card)/.94);box-shadow:var(--glass-highlight)}.trash-table__head,.trash-row{display:grid;grid-template-columns:minmax(220px,1.6fr) minmax(170px,.85fr) minmax(240px,1.15fr) 150px;align-items:center;column-gap:20px;padding:0 18px}.trash-table__head{min-height:38px;border-bottom:1px solid hsl(var(--border));background:hsl(var(--muted)/.28);color:hsl(var(--muted-foreground));font-size:10px;font-weight:700;letter-spacing:.04em}.trash-row{min-height:82px;border-bottom:1px solid hsl(var(--border)/.75);transition:background .16s ease}.trash-row:last-child{border-bottom:0}.trash-row:hover{background:hsl(var(--primary)/.025)}.trash-row__project{display:flex;align-items:center;gap:12px;min-width:0}.trash-card__icon,.trash-empty__icon{display:grid;place-items:center;width:36px;height:36px;flex:none;border:1px solid hsl(var(--destructive)/.12);border-radius:10px;background:linear-gradient(145deg,hsl(var(--destructive)/.08),hsl(var(--destructive)/.035));color:hsl(var(--destructive))}.trash-card__copy{min-width:0}.trash-card__copy h3{overflow:hidden;font-size:13px;font-weight:700;text-overflow:ellipsis;white-space:nowrap}.trash-card__copy p{margin-top:4px;font-size:10px}.trash-row__date{display:grid;gap:4px;color:hsl(var(--muted-foreground));font-size:11px;font-variant-numeric:tabular-nums}.trash-row__date>span{display:none}.trash-row__retention{display:grid;gap:6px;min-width:0}.trash-row__retention-label{display:flex;align-items:center;justify-content:space-between;gap:8px}.trash-row__retention-label strong{font-size:11px;font-weight:650;color:hsl(var(--foreground))}.trash-row__retention-label.is-urgent strong{color:hsl(var(--destructive))}.trash-retention-pill{display:inline-flex;align-items:center;gap:5px;color:hsl(164 74% 35%);font-size:10px;font-weight:700}.trash-retention-pill i{width:6px;height:6px;border-radius:50%;background:currentColor}.is-urgent .trash-retention-pill{color:hsl(var(--destructive))}.trash-retention-track{height:4px;overflow:hidden;border-radius:999px;background:hsl(var(--primary)/.11)}.trash-retention-track span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,hsl(164 68% 43%),hsl(183 70% 42%));transition:width .3s ease}.trash-retention-track.is-urgent{background:hsl(var(--destructive)/.12)}.trash-retention-track.is-urgent span{background:linear-gradient(90deg,hsl(35 90% 52%),hsl(var(--destructive)))}.trash-row__retention small{overflow:hidden;color:hsl(var(--muted-foreground));font-size:10px;text-overflow:ellipsis;white-space:nowrap}.trash-card__actions{display:flex;justify-content:flex-end}.trash-card__actions button{min-width:116px}.trash-card__expired{color:hsl(var(--muted-foreground));font-size:10px;text-align:right}.trash-empty{display:grid;justify-items:center;gap:9px;padding:44px 18px;text-align:center}.trash-empty h3{font-size:15px;font-weight:700}.trash-alert{border:1px solid hsl(var(--destructive)/.25);border-radius:10px;background:hsl(var(--destructive)/.06);padding:10px 12px;color:hsl(var(--destructive));font-size:12px}.trash-skeleton{display:grid;gap:7px;min-height:64px;align-content:center;padding:12px 18px;border-bottom:1px solid hsl(var(--border)/.75)}.skeleton-line{height:10px;border-radius:6px;background:hsl(var(--muted));animation:pulse 1.4s ease-in-out infinite}.trash-skeleton .w-2\/5{width:34%}.trash-skeleton .w-4\/5{width:52%}@keyframes pulse{50%{opacity:.4}}@media(max-width:900px){.trash-table__head,.trash-row{grid-template-columns:minmax(190px,1.3fr) minmax(210px,1fr) 132px;column-gap:14px;padding-right:14px;padding-left:14px}.trash-table__head span:nth-child(2),.trash-row__date{display:none}}@media(max-width:620px){.trash-heading{align-items:flex-start;flex-direction:column}.trash-filters{width:100%}.trash-search{width:auto;flex:1}.trash-table__head{display:none}.trash-row{grid-template-columns:minmax(0,1fr) auto;gap:12px;padding:14px;min-height:0}.trash-row__project{grid-column:1}.trash-row__date{display:grid;grid-column:1;grid-row:2;padding-left:48px}.trash-row__date>span{display:block}.trash-row__retention{grid-column:1/-1;grid-row:3;padding-left:48px}.trash-card__actions{grid-column:2;grid-row:1/3;align-self:center}.trash-card__actions button{min-width:0;padding-right:10px;padding-left:10px}.trash-card__actions button :deep(svg){display:none}}@media(prefers-reduced-motion:reduce){.trash-row,.trash-retention-track span{transition:none}}
</style>
