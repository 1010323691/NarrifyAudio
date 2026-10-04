<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Archive, ChevronLeft, ChevronRight, Music4, RefreshCw, Search, Trash2 } from 'lucide-vue-next'
import Button from '@/components/ui/Button.vue'
import Card from '@/components/ui/Card.vue'
import WorkbenchContextBar from '@/components/WorkbenchContextBar.vue'
import { useProjectStore } from '@/stores/project'
import { cleanupProjectTemp, getProjectSummary, type ProjectFileSummary, type ProjectStorageSummary } from '@/api/project'
import { listProjectFiles } from '@/api/projectFiles'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import { formatBytes as formatBytesBase, type BytesFormat } from '@/utils/format'

interface ResourceProject {
  id: string
  name: string
  summary: ProjectStorageSummary
  scope: 'workspace' | 'registered'
  error?: string
}

const project = useProjectStore()
const { push: toast } = useToast()
const records = ref<ResourceProject[]>([])
const loading = ref(true)
const refreshing = ref(false)
const pageError = ref('')
const search = ref('')
const selectedKind = ref('all')
const selectedProject = ref('all')
const page = ref(1)
const pageSize = 20
const cleaningId = ref('')
const MODULE_LABELS: Record<string, string> = {
  '00_temp': '临时缓存',
  '01_input': '原始文件',
  '02_split_text': '章节文本',
  '03_parsed_json': '解析结果',
  '04_voice_profiles': '角色资料',
  '05_audio_chunk': '合成音频',
  '06_audio_merge': '合并音频',
  '07_output': '最终成品',
  '08_bgm': '背景音乐',
  config: '项目配置',
  logs: '项目日志',
  other: '其他文件',
}

const kinds = computed(() => {
  const values = new Map<string, string>()
  for (const record of records.value) for (const category of record.summary.categories) values.set(category.key, category.label)
  return [{ key: 'all', label: '全部' }, ...Array.from(values, ([key, label]) => ({ key, label }))]
})
const totalBytes = computed(() => records.value.reduce((sum, record) => sum + record.summary.size_bytes, 0))
const totalFiles = computed(() => records.value.reduce((sum, record) => sum + record.summary.file_count, 0))
const projectOptions = computed(() => records.value.map(({ id, name }) => ({ id, name })))
const allScanned = computed(() => records.value.length > 0 && records.value.every((record) => record.scope === 'workspace'))
const fileRows = computed(() => records.value.flatMap((record) => record.summary.recent_files.map((file) => ({ ...file, projectId: record.id, projectName: record.name })))
  .filter((file) => (selectedKind.value === 'all' || file.module === selectedKind.value)
    && (selectedProject.value === 'all' || file.projectId === selectedProject.value)
    && (!search.value.trim() || `${file.name} ${file.relative_path} ${file.projectName}`.toLowerCase().includes(search.value.trim().toLowerCase())))
  .sort((a, b) => Date.parse(b.modified_at) - Date.parse(a.modified_at)))
const recentOutputs = computed(() => records.value.flatMap((record) => record.summary.recent_outputs.map((file) => ({ ...file, projectName: record.name })))
  .sort((a, b) => Date.parse(b.modified_at) - Date.parse(a.modified_at)).slice(0, 8))
const pagedRows = computed(() => fileRows.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const pageCount = computed(() => Math.max(1, Math.ceil(fileRows.value.length / pageSize)))
watch([search, selectedKind, selectedProject], () => { page.value = 1 })

// 本页大小口径：零/负值统一 0 B，B 档取整，KB 及以上 1 位小数
const RESOURCE_BYTES: BytesFormat = { emptyText: '0 B', lowRange: 'clamp', decimals: 'always-one', nonFiniteText: '0 B' }
function formatBytes(value: number) {
  return formatBytesBase(value, RESOURCE_BYTES)
}

function formatDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString()
}

async function load() {
  loading.value = records.value.length === 0
  refreshing.value = true
  pageError.value = ''
  await project.refresh()
  const results = await Promise.all(project.projects.map(async (item): Promise<ResourceProject> => {
    try {
      return { id: item.id, name: item.name, summary: await getProjectSummary(item.id), scope: 'workspace' }
    } catch {
      try {
        const files = await listProjectFiles(item.id)
        const summaryFiles: ProjectFileSummary[] = files.map((file) => ({
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
          id: item.id,
          name: item.name,
          scope: 'registered',
          summary: {
            project_id: item.id, name: item.name, updated_at: item.updated_at,
            file_count: summaryFiles.length, size_bytes: summaryFiles.reduce((sum, file) => sum + file.size_bytes, 0), split_volume_count: 0,
          categories: Array.from(totals, ([key, value]) => ({ key, label: MODULE_LABELS[key] || '其他文件', ...value })),
            recent_files: summaryFiles, recent_outputs: audio,
            cleanup_candidates: { count: 0, size_bytes: 0, older_than_days: 7, blocked_by_active_tasks: false },
          },
          error: '仅统计已登记文件',
        }
      } catch (cause: any) {
        return {
          id: item.id,
          name: item.name,
          scope: 'registered',
          summary: {
            project_id: item.id, name: item.name, updated_at: item.updated_at,
            file_count: 0, size_bytes: 0, split_volume_count: 0, categories: [], recent_files: [], recent_outputs: [],
            cleanup_candidates: { count: 0, size_bytes: 0, older_than_days: 7, blocked_by_active_tasks: false },
          },
          error: cause?.message || '无法读取此项目资源',
        }
      }
    }
  }))
  records.value = results
  page.value = 1
  if (!project.projects.length && project.error) pageError.value = project.error
  refreshing.value = false
  loading.value = false
}

async function cleanup(record: ResourceProject) {
  const candidate = record.summary.cleanup_candidates
  if (!candidate.count || candidate.blocked_by_active_tasks || cleaningId.value) return
  const message = `将清理「${record.name}」中超过 ${candidate.older_than_days} 天的 ${candidate.count} 个临时缓存文件（${formatBytes(candidate.size_bytes)}）。不会删除原文、章节、角色或成品。继续吗？`
  if (!await showConfirm(message, { title: '清理项目缓存', destructive: true })) return
  cleaningId.value = record.id
  try {
    const result = await cleanupProjectTemp(record.id)
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
  <div class="my-resources viewport-page">
    <WorkbenchContextBar>
      <template #icon><Archive /></template>
      <template #title>我的资源</template>
      <template #description>按项目查看生成文件、存储占用和可安全清理的临时缓存。</template>
      <template #metrics>
        <div class="workbench-context-metric"><strong>{{ loading ? '…' : project.projects.length }}</strong>项目</div>
        <div class="workbench-context-metric"><strong>{{ loading ? '…' : totalFiles }}</strong>文件</div>
        <div class="workbench-context-metric"><strong>{{ loading ? '…' : formatBytes(totalBytes) }}</strong>存储占用</div>
      </template>
      <template #actions><Button variant="outline" :disabled="refreshing" @click="load"><RefreshCw class="h-4 w-4" :class="refreshing ? 'animate-spin' : ''" />{{ refreshing ? '刷新中' : '刷新' }}</Button></template>
    </WorkbenchContextBar>

    <div class="page-region" role="region" aria-label="页面工作区" tabindex="0">
      <div v-if="pageError" class="flex items-center justify-between gap-3 rounded-lg border border-destructive/25 bg-destructive/5 px-3 py-2.5 text-sm text-destructive" role="alert">
        <span>{{ pageError }}</span>
        <Button variant="outline" size="sm" @click="load">重试</Button>
      </div>

      <div class="resource-scope-note"><span>{{ allScanned ? '已扫描项目工作空间' : '部分项目仅提供已登记文件统计' }}</span><span>文件清单最多显示每个项目最近 100 项；容量按项目汇总统计</span></div>

      <section class="space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex flex-wrap items-baseline gap-2">
            <h2 class="text-[15px] font-bold">最近生成</h2>
            <span class="text-xs text-muted-foreground">音频、合并结果和最终成品</span>
          </div>
        </div>
        <Card v-if="loading" class="p-6 text-center text-sm text-muted-foreground">正在读取项目文件…</Card>
        <Card v-else-if="recentOutputs.length" class="px-3.5">
          <div v-for="file in recentOutputs" :key="`${file.projectName}-${file.relative_path}`" class="flex items-center gap-3 border-b py-2.5 last:border-0">
            <div class="grid h-[34px] w-[34px] shrink-0 place-items-center rounded-lg bg-primary/10 text-primary"><span class="sr-only">音频文件</span><Music4 class="h-4 w-4" aria-hidden="true" /></div>
            <div class="min-w-0 flex-1">
              <strong class="block truncate text-xs">{{ file.name }}</strong>
              <small class="mt-0.5 block text-[10px] text-muted-foreground">{{ file.projectName }} · {{ MODULE_LABELS[file.module] || '其他文件' }}</small>
            </div>
            <span class="shrink-0 text-[10px] text-muted-foreground">{{ formatBytes(file.size_bytes) }}</span>
            <time class="shrink-0 text-[10px] whitespace-nowrap text-muted-foreground">{{ formatDate(file.modified_at) }}</time>
          </div>
        </Card>
        <Card v-else class="p-6 text-center text-sm text-muted-foreground">还没有生成音频。完成制作后，成品会显示在这里。</Card>
      </section>

      <section class="space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex flex-wrap items-baseline gap-2">
            <h2 class="text-[15px] font-bold">项目文件</h2>
            <span class="text-xs text-muted-foreground">最近更新的文件</span>
          </div>
          <div class="flex w-full flex-wrap items-center gap-2 sm:w-auto">
            <div class="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-lg border border-input px-2.5 text-muted-foreground sm:min-w-[230px] sm:flex-none">
              <Search class="h-4 w-4" />
              <input v-model="search" aria-label="搜索我的文件" placeholder="搜索项目或文件名" class="w-full bg-transparent text-xs text-foreground outline-none" />
            </div>
            <select v-model="selectedKind" aria-label="按类型筛选" class="h-9 min-w-[130px] rounded-lg border border-input bg-background px-2.5 text-xs">
              <option v-for="kind in kinds" :key="kind.key" :value="kind.key">{{ kind.label }}</option>
            </select>
            <select v-model="selectedProject" aria-label="按项目筛选" class="h-9 min-w-[150px] rounded-lg border border-input bg-background px-2.5 text-xs">
              <option value="all">全部项目</option><option v-for="item in projectOptions" :key="item.id" :value="item.id">{{ item.name }}</option>
            </select>
          </div>
        </div>
        <Card v-if="loading" class="p-6 text-center text-sm text-muted-foreground">正在加载…</Card>
        <Card v-else-if="fileRows.length" class="overflow-hidden">
          <div class="w-full overflow-x-auto">
            <table class="workbench-table w-full min-w-[720px] text-xs">
              <thead>
                <tr class="border-b text-left">
                  <th class="max-w-[250px] whitespace-nowrap px-3 py-2.5 font-medium text-muted-foreground">文件</th>
                  <th class="max-w-[250px] whitespace-nowrap px-3 py-2.5 font-medium text-muted-foreground">项目</th>
                  <th class="whitespace-nowrap px-3 py-2.5 font-medium text-muted-foreground">类型</th>
                  <th class="px-3 py-2.5 font-medium text-muted-foreground">大小</th>
                  <th class="px-3 py-2.5 font-medium text-muted-foreground">最近修改</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="file in pagedRows" :key="`${file.projectName}-${file.relative_path}`" class="border-b align-middle last:border-0">
                  <td class="max-w-[250px] px-3 py-2.5"><strong class="block truncate font-medium">{{ file.name }}</strong></td>
                  <td class="max-w-[250px] truncate px-3 py-2.5">{{ file.projectName }}</td>
                  <td class="whitespace-nowrap px-3 py-2.5">{{ kinds.find((kind) => kind.key === file.module)?.label || MODULE_LABELS[file.module] || '其他文件' }}</td>
                  <td class="whitespace-nowrap px-3 py-2.5">{{ formatBytes(file.size_bytes) }}</td>
                  <td class="whitespace-nowrap px-3 py-2.5">{{ formatDate(file.modified_at) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="resource-pagination"><span>第 {{ fileRows.length ? (page - 1) * pageSize + 1 : 0 }}–{{ Math.min(page * pageSize, fileRows.length) }} 条，共 {{ fileRows.length }} 项（每项目最多读取最近 100 项）</span><div><Button size="sm" variant="outline" :disabled="page <= 1" aria-label="上一页" @click="page--"><ChevronLeft class="h-4 w-4" /></Button><span>{{ page }} / {{ pageCount }}</span><Button size="sm" variant="outline" :disabled="page >= pageCount" aria-label="下一页" @click="page++"><ChevronRight class="h-4 w-4" /></Button></div></div>
        </Card>
        <Card v-else class="p-6 text-center text-sm text-muted-foreground">没有找到匹配文件。</Card>
      </section>

      <section class="space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div class="flex flex-wrap items-baseline gap-2">
            <h2 class="text-[15px] font-bold">可清理临时缓存</h2>
            <span class="text-xs text-muted-foreground">超过 7 天且项目没有活动任务的临时文件</span>
          </div>
        </div>
        <Card v-if="!records.length && !loading" class="p-6 text-center text-sm text-muted-foreground">创建项目后，这里会显示可清理空间。</Card>
        <Card v-else class="px-3.5">
          <div v-for="record in records" :key="record.id" class="flex items-center justify-between gap-3 border-b py-2.5 last:border-0">
            <div class="min-w-0">
              <strong class="block text-xs">{{ record.name }}</strong>
              <small v-if="record.summary.cleanup_candidates.blocked_by_active_tasks" class="mt-0.5 block text-[10px] text-muted-foreground">项目任务正在运行，暂不可清理</small>
              <small v-else-if="record.summary.cleanup_candidates.count" class="mt-0.5 block text-[10px] text-muted-foreground">{{ record.summary.cleanup_candidates.count }} 个文件 · {{ formatBytes(record.summary.cleanup_candidates.size_bytes) }}</small>
              <small v-else class="mt-0.5 block text-[10px] text-muted-foreground">{{ record.error || '没有过期临时缓存' }}</small>
            </div>
            <Button variant="outline" size="sm" class="shrink-0" :disabled="!record.summary.cleanup_candidates.count || record.summary.cleanup_candidates.blocked_by_active_tasks || cleaningId === record.id" @click="cleanup(record)"><Trash2 class="h-4 w-4" />{{ cleaningId === record.id ? '清理中…' : '清理缓存' }}</Button>
          </div>
        </Card>
      </section>
    </div>
  </div>
</template>

<style scoped>
.my-resources{gap:10px}.resource-scope-note{display:flex;justify-content:space-between;gap:12px;padding:9px 12px;border:1px solid hsl(var(--border));border-radius:9px;background:hsl(var(--muted)/.25);color:hsl(var(--muted-foreground));font-size:11px}.resource-pagination{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 12px;border-top:1px solid hsl(var(--border));color:hsl(var(--muted-foreground));font-size:11px}.resource-pagination>div{display:flex;align-items:center;gap:8px}.resource-pagination button{width:30px;height:30px;padding:0}@media(max-width:700px){.resource-scope-note{flex-direction:column;gap:3px}.resource-pagination{align-items:flex-start;flex-direction:column}}
</style>
