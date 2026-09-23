<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, Search, Trash2 } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import { useWorkspaceStore } from '@/stores/workspace'
import { cleanupWorkspaceTemp, getWorkspaceSummary, type WorkspaceFileSummary, type WorkspaceSummary } from '@/api/workspace'
import { listProjectFiles } from '@/api/projects'
import { useToast } from '@/components/ui/toast'

interface ResourceProject {
  id: string
  name: string
  summary: WorkspaceSummary
  scope: 'workspace' | 'registered'
  error?: string
}

const workspace = useWorkspaceStore()
const { push: toast } = useToast()
const records = ref<ResourceProject[]>([])
const loading = ref(true)
const refreshing = ref(false)
const pageError = ref('')
const search = ref('')
const selectedKind = ref('all')
const cleaningId = ref('')

const kinds = computed(() => {
  const values = new Map<string, string>()
  for (const record of records.value) for (const category of record.summary.categories) values.set(category.key, category.label)
  return [{ key: 'all', label: '全部' }, ...Array.from(values, ([key, label]) => ({ key, label }))]
})
const totalBytes = computed(() => records.value.reduce((sum, record) => sum + record.summary.size_bytes, 0))
const totalFiles = computed(() => records.value.reduce((sum, record) => sum + record.summary.file_count, 0))
const allScanned = computed(() => records.value.length > 0 && records.value.every((record) => record.scope === 'workspace'))
const fileRows = computed(() => records.value.flatMap((record) => record.summary.recent_files.map((file) => ({ ...file, projectName: record.name })))
  .filter((file) => (selectedKind.value === 'all' || file.module === selectedKind.value)
    && (!search.value.trim() || `${file.name} ${file.relative_path} ${file.projectName}`.toLowerCase().includes(search.value.trim().toLowerCase())))
  .sort((a, b) => Date.parse(b.modified_at) - Date.parse(a.modified_at)))
const recentOutputs = computed(() => records.value.flatMap((record) => record.summary.recent_outputs.map((file) => ({ ...file, projectName: record.name })))
  .sort((a, b) => Date.parse(b.modified_at) - Date.parse(a.modified_at)).slice(0, 8))

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  return `${(value / (1024 ** index)).toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

function formatDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
}

async function load() {
  loading.value = records.value.length === 0
  refreshing.value = true
  pageError.value = ''
  await workspace.refresh()
  const results = await Promise.all(workspace.projects.map(async (project): Promise<ResourceProject> => {
    try {
      return { id: project.id, name: project.name, summary: await getWorkspaceSummary(project.id), scope: 'workspace' }
    } catch {
      try {
        const files = await listProjectFiles(project.id)
        const summaryFiles: WorkspaceFileSummary[] = files.map((file) => ({
          name: file.name,
          relative_path: file.name,
          module: file.module || 'other',
          size_bytes: file.size_bytes,
          modified_at: file.created_at,
        }))
        const totals = new Map<string, { count: number; size_bytes: number }>()
        for (const file of summaryFiles) {
          const bucket = totals.get(file.module) || { count: 0, size_bytes: 0 }
          bucket.count += 1
          bucket.size_bytes += file.size_bytes
          totals.set(file.module, bucket)
        }
        const audio = summaryFiles.filter((file) => /\.(mp3|wav|flac|m4a|aac|ogg|opus|zip)$/i.test(file.name))
        return {
          id: project.id,
          name: project.name,
          scope: 'registered',
          summary: {
            workspace_id: project.id, name: project.name, updated_at: project.updated_at,
            file_count: summaryFiles.length, size_bytes: summaryFiles.reduce((sum, file) => sum + file.size_bytes, 0),
            categories: Array.from(totals, ([key, value]) => ({ key, label: key, ...value })),
            recent_files: summaryFiles, recent_outputs: audio,
            cleanup_candidates: { count: 0, size_bytes: 0, older_than_days: 7, blocked_by_active_tasks: false },
          },
          error: '仅统计已登记文件',
        }
      } catch (cause: any) {
        return {
          id: project.id,
          name: project.name,
          scope: 'registered',
          summary: {
            workspace_id: project.id, name: project.name, updated_at: project.updated_at,
            file_count: 0, size_bytes: 0, categories: [], recent_files: [], recent_outputs: [],
            cleanup_candidates: { count: 0, size_bytes: 0, older_than_days: 7, blocked_by_active_tasks: false },
          },
          error: cause?.message || '无法读取此项目资源',
        }
      }
    }
  }))
  records.value = results
  if (!workspace.projects.length && workspace.error) pageError.value = workspace.error
  refreshing.value = false
  loading.value = false
}

async function cleanup(record: ResourceProject) {
  const candidate = record.summary.cleanup_candidates
  if (!candidate.count || candidate.blocked_by_active_tasks || cleaningId.value) return
  const message = `将清理「${record.name}」中超过 ${candidate.older_than_days} 天的 ${candidate.count} 个临时缓存文件（${formatBytes(candidate.size_bytes)}）。不会删除原文、章节、角色或成品。继续吗？`
  if (!window.confirm(message)) return
  cleaningId.value = record.id
  try {
    const result = await cleanupWorkspaceTemp(record.id)
    toast({ title: '临时缓存已清理', variant: 'success', description: `删除 ${result.deleted_count} 个文件，释放 ${formatBytes(result.deleted_bytes)}。` })
    await load()
  } catch (cause: any) {
    toast({ title: '清理失败', variant: 'destructive', description: cause?.message || '请稍后重试。' })
  } finally {
    cleaningId.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="resources-page">
    <header class="page-header resources-header">
      <div><p class="eyebrow">YOUR LIBRARY</p><h1 class="page-title">我的资源</h1><p class="page-description">只显示你自己的项目文件和生成内容。</p></div>
      <Button variant="outline" :disabled="refreshing" @click="load"><RefreshCw class="h-4 w-4" />刷新</Button>
    </header>

    <div v-if="pageError" class="resource-alert" role="alert">{{ pageError }} <Button variant="outline" size="sm" @click="load">重试</Button></div>
    <div class="resource-stats">
      <Card><span>我的项目</span><strong>{{ workspace.projects.length }}</strong></Card>
      <Card><span>文件数量</span><strong>{{ loading ? '…' : totalFiles }}</strong></Card>
      <Card><span>存储占用</span><strong>{{ loading ? '…' : formatBytes(totalBytes) }}</strong><small>{{ allScanned ? '已扫描项目工作空间' : '部分后端仅提供已登记文件统计' }}</small></Card>
    </div>

    <section class="resources-section">
      <div class="section-heading"><div><h2>最近生成</h2><span class="muted">音频、合并结果和最终成品</span></div></div>
      <Card v-if="loading" class="resource-empty">正在读取项目文件…</Card>
      <Card v-else-if="recentOutputs.length" class="outputs-list">
        <div v-for="file in recentOutputs" :key="`${file.projectName}-${file.relative_path}`" class="output-row">
          <div class="output-icon"><span class="sr-only">音频文件</span>♫</div>
          <div class="output-copy"><strong>{{ file.name }}</strong><small>{{ file.projectName }} · {{ file.module }}</small></div>
          <span>{{ formatBytes(file.size_bytes) }}</span><time>{{ formatDate(file.modified_at) }}</time>
        </div>
      </Card>
      <Card v-else class="resource-empty">还没有生成音频。完成制作后，成品会显示在这里。</Card>
    </section>

    <section class="resources-section">
      <div class="section-heading resource-files-heading">
        <div><h2>项目文件</h2><span class="muted">最近更新的文件</span></div>
        <div class="resource-controls">
          <div class="resource-search"><Search class="h-4 w-4" /><input v-model="search" aria-label="搜索我的文件" placeholder="搜索项目或文件名" /></div>
          <select v-model="selectedKind" aria-label="按类型筛选">
            <option v-for="kind in kinds" :key="kind.key" :value="kind.key">{{ kind.label }}</option>
          </select>
        </div>
      </div>
      <Card v-if="loading" class="resource-empty">正在加载…</Card>
      <Card v-else-if="fileRows.length" class="resource-table-card">
        <div class="resource-table"><table><thead><tr><th>文件</th><th>项目</th><th>类型</th><th>大小</th><th>最近修改</th></tr></thead>
          <tbody><tr v-for="file in fileRows.slice(0, 100)" :key="`${file.projectName}-${file.relative_path}`"><td><strong>{{ file.name }}</strong><small>{{ file.relative_path }}</small></td><td>{{ file.projectName }}</td><td>{{ kinds.find((kind) => kind.key === file.module)?.label || file.module }}</td><td>{{ formatBytes(file.size_bytes) }}</td><td>{{ formatDate(file.modified_at) }}</td></tr></tbody>
        </table></div>
        <p v-if="fileRows.length > 100" class="table-footnote">显示最近 100 个匹配文件。</p>
      </Card>
      <Card v-else class="resource-empty">没有找到匹配文件。</Card>
    </section>

    <section class="resources-section">
      <div class="section-heading"><div><h2>可清理临时缓存</h2><span class="muted">超过 7 天且项目没有活动任务的临时文件</span></div></div>
      <Card v-if="!records.length && !loading" class="resource-empty">创建项目后，这里会显示可清理空间。</Card>
      <Card v-else class="cleanup-list">
        <div v-for="record in records" :key="record.id" class="cleanup-row">
          <div><strong>{{ record.name }}</strong><small v-if="record.summary.cleanup_candidates.blocked_by_active_tasks">项目任务正在运行，暂不可清理</small><small v-else-if="record.summary.cleanup_candidates.count">{{ record.summary.cleanup_candidates.count }} 个文件 · {{ formatBytes(record.summary.cleanup_candidates.size_bytes) }}</small><small v-else>{{ record.error || '没有过期临时缓存' }}</small></div>
          <Button variant="outline" size="sm" :disabled="!record.summary.cleanup_candidates.count || record.summary.cleanup_candidates.blocked_by_active_tasks || cleaningId === record.id" @click="cleanup(record)"><Trash2 class="h-4 w-4" />{{ cleaningId === record.id ? '清理中…' : '清理缓存' }}</Button>
        </div>
      </Card>
    </section>
  </div>
</template>

<style scoped>
.resources-page{display:grid;gap:24px;max-width:1320px;margin:0 auto;padding-bottom:30px}.resources-header{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap}.eyebrow{font-size:10px;font-weight:800;letter-spacing:.14em;color:hsl(var(--primary))}.resources-header h1{margin-top:5px}.resources-header .page-description{margin-top:4px}.resource-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.resource-stats>*{display:grid;gap:4px;padding:14px}.resource-stats span,.resource-stats small,.muted,.table-footnote{color:hsl(var(--muted-foreground));font-size:11px}.resource-stats strong{font-size:20px;font-weight:750;font-variant-numeric:tabular-nums}.resources-section{display:grid;gap:11px}.section-heading{display:flex;justify-content:space-between;align-items:center;gap:12px}.section-heading>div:first-child{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}.section-heading h2{font-size:15px;font-weight:750}.outputs-list,.cleanup-list{padding:0 14px}.output-row,.cleanup-row{display:flex;align-items:center;gap:12px;border-bottom:1px solid hsl(var(--border));padding:11px 0}.output-row:last-child,.cleanup-row:last-child{border-bottom:0}.output-icon{display:grid;place-items:center;width:34px;height:34px;flex:none;border-radius:9px;background:hsl(var(--primary)/.09);color:hsl(var(--primary));font-size:18px}.output-copy{min-width:0;flex:1}.output-copy strong,.output-copy small,.cleanup-row strong,.cleanup-row small{display:block}.output-copy strong{overflow:hidden;font-size:12px;text-overflow:ellipsis;white-space:nowrap}.output-copy small,.cleanup-row small{margin-top:3px;color:hsl(var(--muted-foreground));font-size:10px}.output-row>span,.output-row time{color:hsl(var(--muted-foreground));font-size:10px;white-space:nowrap}.resource-files-heading{align-items:flex-end;flex-wrap:wrap}.resource-controls{display:flex;gap:8px;flex-wrap:wrap}.resource-search{display:flex;align-items:center;gap:7px;min-width:230px;height:36px;border:1px solid hsl(var(--input));border-radius:8px;padding:0 9px;color:hsl(var(--muted-foreground))}.resource-search input{width:100%;border:0;outline:0;background:transparent;color:hsl(var(--foreground));font-size:12px}.resource-controls select{height:36px;min-width:130px;border:1px solid hsl(var(--input));border-radius:8px;background:hsl(var(--background));padding:0 9px;font-size:12px}.resource-table-card{overflow:hidden}.resource-table{width:100%;overflow-x:auto}table{width:100%;min-width:720px;border-collapse:collapse;font-size:11px}th,td{padding:10px 12px;border-bottom:1px solid hsl(var(--border));text-align:left;vertical-align:middle}th{color:hsl(var(--muted-foreground));font-weight:650;white-space:nowrap}td{max-width:250px}td strong,td small{display:block}td strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px}td small{overflow:hidden;margin-top:3px;color:hsl(var(--muted-foreground));font-size:9px;text-overflow:ellipsis;white-space:nowrap}.table-footnote{padding:9px 12px}.resource-empty{padding:22px;text-align:center;color:hsl(var(--muted-foreground));font-size:12px}.cleanup-row{justify-content:space-between}.cleanup-row>div{min-width:0}.cleanup-row strong{font-size:12px}.resource-alert{display:flex;align-items:center;justify-content:space-between;gap:12px;border:1px solid hsl(var(--destructive)/.25);border-radius:9px;padding:10px 12px;color:hsl(var(--destructive));font-size:12px}@media(max-width:750px){.resource-stats{grid-template-columns:1fr}.output-row{flex-wrap:wrap}.output-copy{min-width:calc(100% - 60px)}.output-row time{margin-left:auto}.resource-controls{width:100%}.resource-search{flex:1}.resource-files-heading{align-items:stretch}}
</style>
