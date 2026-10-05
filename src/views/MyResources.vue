<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Archive, ArrowLeft, ArrowUpRight, Check, ChevronDown, ChevronUp, RefreshCw, Wifi, WifiOff, X, Download, Package } from 'lucide-vue-next'
import { useResourceCenter } from '@/composables/useResourceCenter'
import ResourceProjectMatrix from '@/components/resources/ResourceProjectMatrix.vue'
import ResourceFileBrowser from '@/components/resources/ResourceFileBrowser.vue'
import ResourceTaskBrief from '@/components/resources/ResourceTaskBrief.vue'
import ResourcePreviewDrawer from '@/components/resources/ResourcePreviewDrawer.vue'
import ResourceActionDialog from '@/components/resources/ResourceActionDialog.vue'
import ResourceStorageView from '@/components/resources/ResourceStorageView.vue'
import { resourceBytes } from '@/utils/resources'
import { resourceFileUrl, resourceExportUrl, type ResourceEntry } from '@/api/resources'
import { useToast } from '@/components/ui/toast'
import '@/styles/resources.css'

const c = useResourceCenter()
const { overview, entries, loading, refreshing, entriesLoading, pageError, entriesError, projectSearch, projectSort, fileSearch, selection, previewEntry, modal, submitting, tab, projectId, browsing, category, path, directoryMode, extension, sort, page, currentProject, projects, completeCount, currentTasks, operationTasks, updatingProjects, actionResult, tasksExpanded, exportSelection, exportAll, exportCount, exportBytes, exportScope, cleanup, cleanupLoading, cleanupPage, allPageSelected } = c
const { push: toast } = useToast()
const root = ref<HTMLElement | null>(null)
const canPin = ref(false)
const pinned = ref(false)
let observer: ResizeObserver | null = null
const activeTaskCount = computed(() => c.task.tasks.filter(item => ['running', 'pending', 'paused'].includes(item.status)).length)
const connected = computed(() => c.task.connectionStatus === 'connected')
const connectionLabel = computed(() => connected.value ? '任务实时同步' : c.task.connectionStatus === 'reconnecting' ? '连接中断，正在重连' : '正在连接任务通道')
const title = computed(() => tab.value === 'storage' ? '存储管理' : browsing.value ? currentProject.value?.name || '跨项目文件' : '我的资源')
function download(entry: ResourceEntry) { if (!entry.can_download) return; const a = document.createElement('a'); a.href = resourceFileUrl(entry.id); a.download = entry.name; a.click() }
async function copy(entry: ResourceEntry) {
  try { await navigator.clipboard.writeText(entry.relative_path); toast({ title: '已复制相对路径' }) }
  catch { toast({ title: '复制失败', description: '请从文件详情选取相对路径。', variant: 'destructive' }) }
}
function resultDownload(item: typeof operationTasks.value[number]) { return item.task_type === 'resources.package' && overview.value?.exports.some(value => value.task_id === item.id && value.available) }
watch([canPin, previewEntry], () => { if (!canPin.value || !previewEntry.value) pinned.value = false })
onMounted(() => { observer = new ResizeObserver(([entry]) => { canPin.value = entry.contentRect.width >= 1320 }); if (root.value) observer.observe(root.value) })
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <div ref="root" class="viewport-page resource-center">
    <header class="rc-header"><div class="rc-header-copy"><span class="rc-header-icon"><Archive :size="22" /></span><div><h1>{{ title }}</h1><p>{{ browsing && tab !== 'storage' ? (tab === 'materials' ? '检查制作资料，前往工作台继续处理' : '试听与下载已完成的有声书成品') : '领取成品、检查制作资料、管理存储占用' }}</p></div></div><div class="rc-header-actions"><span class="rc-connection" :class="{ 'is-reconnecting': !connected }" role="status"><Wifi v-if="connected" :size="13" /><WifiOff v-else :size="13" />{{ connectionLabel }}</span><button class="rc-button" :disabled="refreshing" @click="c.refreshResources(projectId || undefined)"><RefreshCw :size="15" :class="{ 'rc-spin': refreshing }" />更新清单</button></div></header>
    <div class="rc-nav"><nav class="rc-tabs" aria-label="资源中心视图"><button :aria-current="tab === 'projects' ? 'page' : undefined" @click="c.setTab('projects')">成品<span>{{ overview?.projects.length ?? '—' }}</span></button><button :aria-current="tab === 'materials' ? 'page' : undefined" @click="c.setTab('materials')">制作资料</button><button :aria-current="tab === 'storage' ? 'page' : undefined" @click="c.setTab('storage')">存储管理</button></nav><span class="rc-overview-meta">{{ completeCount }}个项目清单完整<span>·</span>{{ overview?.projects.reduce((sum, item) => sum + item.delivery_count, 0) || 0 }}份可下载成品<span>·</span>{{ overview?.storage.project_complete === false ? '≥ ' : '' }}{{ resourceBytes(overview?.storage.project_bytes) }}</span></div>
    <div class="page-region rc-page-region" tabindex="0" aria-label="资源中心内容">
      <div v-if="pageError" class="rc-alert" role="alert"><span>{{ pageError }}</span><button class="rc-text-link" @click="c.loadOverview(true)">重试</button></div>
      <div v-if="actionResult" class="rc-notice rc-result" role="status"><Check :size="16" /><span>{{ actionResult }}</span><button class="rc-icon-button" aria-label="关闭操作提示" @click="actionResult = ''"><X :size="15" /></button></div>
      <section v-if="activeTaskCount" class="rc-live-panel" aria-label="当前任务"><div class="rc-live-heading"><span><i class="rc-live-dot" />{{ tab !== 'storage' && browsing && currentProject ? '此项目任务' : '当前制作任务' }}<strong>{{ currentTasks.length }}</strong><small>任务结束后自动更新资源</small></span><button class="rc-text-link" :aria-expanded="tasksExpanded" @click="tasksExpanded = !tasksExpanded">{{ tasksExpanded ? '收起' : '展开进度' }}<ChevronUp v-if="tasksExpanded" :size="14" /><ChevronDown v-else :size="14" /></button></div><ResourceTaskBrief v-if="tasksExpanded" :tasks="currentTasks" detailed :limit="6" /><p v-if="!currentTasks.length" class="rc-muted">其他项目有{{ activeTaskCount }}个活动任务。<RouterLink to="/tasks" class="rc-text-link">查看任务中心<ArrowUpRight :size="12" /></RouterLink></p></section>
      <section v-if="operationTasks.length" class="rc-operations" aria-label="资源操作结果"><div v-for="item in operationTasks" :key="item.id" class="rc-operation"><Package :size="15" /><span class="rc-operation-name">{{ item.label }}</span><span :class="{ 'rc-warning-text': item.status === 'failed' }">{{ item.status === 'succeeded' ? '已完成' : item.status === 'failed' ? '失败，请查看原因' : item.status === 'cancelled' ? '已取消' : item.status === 'paused' ? '已暂停' : item.status === 'running' ? `进行中 ${Math.round(item.progress * 100)}%` : '等待执行' }}</span><a v-if="resultDownload(item)" class="rc-text-link" :href="resourceExportUrl(item.id)"><Download :size="14" />下载ZIP</a><RouterLink v-else class="rc-text-link" to="/tasks">查看任务<ArrowUpRight :size="12" /></RouterLink></div></section>
      <ResourceStorageView v-if="tab === 'storage'" :overview="overview" :loading="loading || refreshing" @cleanup="c.prepareCleanup" @refresh="c.refreshResources()" />
      <template v-else-if="browsing"><div class="rc-file-heading"><button class="rc-text-link" @click="c.backToProjects"><ArrowLeft :size="14" />全部项目</button><div><span v-if="currentProject?.scan_error" class="rc-warning-text">{{ currentProject.scan_error }}</span><span v-else-if="projectId && updatingProjects.has(projectId)" class="rc-sync-label"><RefreshCw :size="12" class="rc-spin" />资源更新中，当前显示上次完整清单</span><button v-if="currentProject" class="rc-text-link" @click="c.enterProduction(projectId)">进入制作<ArrowUpRight :size="14" /></button></div></div><div class="rc-files-and-preview" :class="{ 'has-pinned-preview': pinned && previewEntry }"><ResourceFileBrowser :materials="tab === 'materials'" :project="currentProject" :projects="overview?.projects || []" :project-id="projectId" :category="category" :path="path" :directory-mode="directoryMode" :search="fileSearch" :extension="extension" :sort="sort" :page="page" :entries="entries" :loading="entriesLoading" :error="entriesError" :selection="selection" :all-selected="allPageSelected" :preview-id="previewEntry?.id" @category="c.selectCategory" @query="c.changeQuery" @search="fileSearch = $event" @directory="c.enterDirectory" @browse-directory="c.browseCategoryDirectory" @preview="previewEntry = $event" @select="c.toggleSelected" @select-page="c.togglePageSelected" @clear-selection="selection = new Set()" @export="c.prepareExport" @download="download" @copy="copy" @retry="c.loadEntries" @page="c.setPage" /><ResourcePreviewDrawer v-if="previewEntry" :entry="previewEntry" :can-pin="canPin" :pinned="pinned" @close="previewEntry = null" @pin="pinned = !pinned" @production="c.enterProduction" @download="download" @copy="copy" /></div></template>
      <ResourceProjectMatrix v-else :materials="tab === 'materials'" :projects="projects" :total-projects="overview?.projects.length || 0" :current-id="c.project.activeProjectId || ''" :loading="loading" v-model:project-search="projectSearch" v-model:project-sort="projectSort" :tasks-for-project="c.tasksForProject" :updating-projects="updatingProjects" @open="c.openProject" @global="c.openGlobalFiles" @production="c.enterProduction" @refresh="c.refreshResources" @export="c.prepareProjectExport" @trash="c.trashProject" />
    </div>
    <ResourceActionDialog v-if="modal" :mode="modal" :submitting="submitting" :export-all="exportAll" :export-files="exportSelection" :export-count="exportCount" :export-bytes="exportBytes" :export-scope="exportScope" :cleanup="cleanup" :cleanup-loading="cleanupLoading" :cleanup-page="cleanupPage" @close="!submitting && (modal = null)" @export="c.confirmExport" @cleanup="c.confirmCleanup" @cleanup-page="c.loadCleanup" />
  </div>
</template>
