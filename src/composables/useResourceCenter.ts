import { computed, onActivated, onBeforeUnmount, onDeactivated, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import { useToast } from '@/components/ui/toast'
import { showConfirm } from '@/components/ui/dialog'
import {
  getResourceProjectIds, getResourceOverview, getResourceEntries, getCleanupPreview, submitResourceTask,
  type ResourceEntry, type ResourceEntries, type ResourceOverview, type ResourceQuery,
  type CleanupPreview, type ResourceProject, type SnapshotReference,
} from '@/api/resources'
import { deleteProject } from '@/api/project'
import { RESOURCE_FILTERS, RESOURCE_STAGES, entryIsSystem, isActiveResourceTask, resourceBytes } from '@/utils/resources'
import type { TaskSnapshot } from '@/types'

export function useResourceCenter() {
  const route = useRoute()
  const router = useRouter()
  const project = useProjectStore()
  const task = useTaskStore()
  const { push: toast } = useToast()
  const projectPage = ref(1)
  const overview = shallowRef<ResourceOverview | null>(null)
  const entries = shallowRef<ResourceEntries | null>(null)
  const loading = ref(true)
  const refreshing = ref(false)
  const entriesLoading = ref(false)
  const pageError = ref('')
  const entriesError = ref('')
  const projectSearch = ref('')
  const projectSort = ref<'recent' | 'size' | 'name'>('recent')
  const fileSearch = ref(String(route.query.q || ''))
  const selection = ref(new Set<string>())
  const previewEntry = shallowRef<ResourceEntry | null>(null)
  const modal = ref<'export' | 'cleanup' | null>(null)
  const exportSelection = shallowRef<ResourceEntry[]>([])
  const exportAll = ref(false)
  const exportScope = shallowRef<ResourceQuery | null>(null)
  const exportReferences = shallowRef<SnapshotReference[]>([])
  const exportCount = ref(0)
  const exportBytes = ref(0)
  const cleanup = shallowRef<CleanupPreview | null>(null)
  const cleanupIds = ref<string[]>([])
  const cleanupPage = ref(1)
  const cleanupLoading = ref(false)
  const submitting = ref(false)
  const updatingProjects = ref(new Set<string>())
  const actionResult = ref('')
  const tasksExpanded = ref(false)
  const recentOperationIds = ref(new Set<string>())
  const busyProjectId = ref('')
  const selectedCleanup = ref(new Set<string>())
  let alive = false
  let openedAt = 0
  let lifecycle = 0
  let overviewSequence = 0
  let entriesSequence = 0
  let cleanupSequence = 0
  let exportSequence = 0
  let releaseScope: (() => void) | null = null
  let overviewAbort: AbortController | null = null
  let entriesAbort: AbortController | null = null
  let cleanupAbort: AbortController | null = null
  let searchTimer: ReturnType<typeof setTimeout> | null = null
  let refreshTimer: ReturnType<typeof setTimeout> | null = null
  const scanAttempts = new Set<string>()
  const pendingRefresh = new Set<string>()
  const seenTasks = new Map<string, string>()
  const submittedScopes = new Map<string, string[]>()

  const tab = computed(() => route.query.tab === 'storage' ? 'storage' : route.query.tab === 'materials' ? 'materials' : 'projects')
  const projectId = computed(() => String(route.query.project || ''))
  const browsing = computed(() => !!projectId.value || route.query.view === 'files')
  const category = computed(() => tab.value !== 'materials' ? 'deliverables' : RESOURCE_FILTERS.some(item => item.key === route.query.category && !['deliverables', 'system', '07_output'].includes(item.key)) ? String(route.query.category) : 'production')
  const path = computed(() => String(route.query.path || ''))
  const directoryMode = computed(() => route.query.mode === 'directory')
  const extension = computed(() => String(route.query.extension || ''))
  const sort = computed<'name' | 'modified' | 'size'>(() => ['modified', 'size'].includes(String(route.query.sort)) ? route.query.sort as 'modified' | 'size' : 'name')
  const page = computed(() => Math.max(1, Number(route.query.page) || 1))
  const currentProject = computed(() => overview.value?.projects.find(item => item.project_id === projectId.value) || null)
  const fileQuery = computed<ResourceQuery>(() => ({
    role: tab.value === 'materials' ? 'production' : 'deliverable',
    project_id: projectId.value || undefined, category: category.value, path: path.value,
    query: String(route.query.q || ''), extension: extension.value, sort: sort.value,
    page: page.value, page_size: 50, directory_mode: directoryMode.value,
  }))
  const activeTasks = computed(() => task.tasks.filter(isActiveResourceTask))
  const currentTasks = computed(() => activeTasks.value.filter(item => tab.value === 'storage' || !projectId.value || item.project_id === projectId.value))
  const selectedFiles = computed(() => entries.value?.items.filter(item => selection.value.has(item.id) && item.can_package) || [])
  const allPageSelected = computed(() => !!entries.value?.items.some(item => item.can_package) && entries.value.items.filter(item => item.can_package).every(item => selection.value.has(item.id)))
  const projects = computed(() => {
    if (overview.value?.pagination) return overview.value.projects
    const filtered = (overview.value?.projects || []).filter(item => item.name.toLocaleLowerCase().includes(projectSearch.value.trim().toLocaleLowerCase()))
    return filtered.slice().sort((a, b) => projectSort.value === 'name' ? a.name.localeCompare(b.name, 'zh', { numeric: true }) : projectSort.value === 'size' ? (b.snapshot?.size_bytes ?? -1) - (a.snapshot?.size_bytes ?? -1) : Date.parse(b.snapshot?.latest_modified_at || '') - Date.parse(a.snapshot?.latest_modified_at || '') || a.name.localeCompare(b.name, 'zh'))
  })
  const completeCount = computed(() => overview.value?.pagination?.counts.complete ?? (overview.value?.projects.filter(item => item.snapshot?.complete && !item.scan_error).length || 0))
  const scanTasks = computed(() => activeTasks.value.filter(item => item.task_type === 'resources.scan'))
  const operationTasks = computed(() => task.tasks.filter(item => ['resources.package', 'resources.cleanup'].includes(item.task_type) && (isActiveResourceTask(item) || recentOperationIds.value.has(item.id))).slice(0, 3))

  function changeQuery(next: Record<string, string | number | undefined>) {
    const query = { ...route.query }
    for (const [key, value] of Object.entries(next)) {
      if (value === undefined || value === '') delete query[key]
      else query[key] = String(value)
    }
    return router.replace({ path: '/resources', query })
  }
  function setTab(next: 'projects' | 'materials' | 'storage') { void changeQuery({ tab: next === 'projects' ? undefined : next, category: undefined, path: undefined, mode: undefined, q: undefined, extension: undefined, page: undefined }) }
  function openProject(id: string, nextCategory = tab.value === 'materials' ? 'production' : 'deliverables') {
    previewEntry.value = null
    void changeQuery({ project: id, view: 'files', category: nextCategory, path: undefined, mode: undefined, q: undefined, extension: undefined, page: undefined, tab: nextCategory === 'deliverables' ? undefined : 'materials' })
  }
  function openGlobalFiles() { previewEntry.value = null; void changeQuery({ project: undefined, view: 'files', category: undefined, path: undefined, mode: undefined, q: undefined, page: undefined }) }
  function backToProjects() { previewEntry.value = null; void router.replace({ path: '/resources', query: tab.value === 'materials' ? { tab: 'materials' } : {} }) }
  function selectCategory(next: string) { void changeQuery({ category: next, path: undefined, mode: undefined, page: undefined }) }
  function enterDirectory(entry: ResourceEntry) { void changeQuery({ project: entry.project_id, path: entry.relative_path, mode: 'directory', q: undefined, page: undefined }) }
  function browseCategoryDirectory() {
    const module = RESOURCE_FILTERS.find(item => item.key === category.value)?.module
    if (module && projectId.value) void changeQuery({ path: module, mode: 'directory', q: undefined, page: undefined })
  }
  function setPage(next: number) { void changeQuery({ page: next }) }
  function toggleSelected(id: string) {
    if (tab.value !== 'projects' || !entries.value?.items.some(item => item.id === id && item.can_package)) return
    const next = new Set(selection.value)
    if (!next.delete(id)) next.add(id)
    selection.value = next
  }
  function togglePageSelected() { selection.value = allPageSelected.value || tab.value !== 'projects' ? new Set() : new Set(entries.value?.items.filter(item => item.can_package).map(item => item.id)) }

  async function loadEntries() {
    if (!alive || !browsing.value || tab.value === 'storage') return
    const sequence = ++entriesSequence
    const currentLifecycle = lifecycle
    entriesAbort?.abort()
    entriesAbort = new AbortController()
    entriesLoading.value = true
    entriesError.value = ''
    try {
      const result = await getResourceEntries(fileQuery.value, { signal: entriesAbort.signal })
      if (!alive || currentLifecycle !== lifecycle || sequence !== entriesSequence) return
      if (entries.value && JSON.stringify(entries.value.snapshots) !== JSON.stringify(result.snapshots)) selection.value = new Set()
      entries.value = result
      const pages = Math.max(1, Math.ceil(result.total / 50))
      if (page.value > pages) setPage(pages)
    } catch (error: any) {
      if (alive && currentLifecycle === lifecycle && sequence === entriesSequence && error?.name !== 'AbortError') entriesError.value = error?.message || '文件清单暂不可用'
    } finally {
      if (alive && currentLifecycle === lifecycle && sequence === entriesSequence) entriesLoading.value = false
    }
  }

  async function scanProjects(ids: string[], manual = false, includeTrash = false) {
    const currentLifecycle = lifecycle
    const targets = [...new Set(ids)]
    if (!targets.length || !alive) return
    targets.forEach(id => scanAttempts.add(id))
    updatingProjects.value = new Set([...updatingProjects.value, ...targets])
    try {
      const result = await submitResourceTask(targets[0], 'resources.scan', { project_ids: targets, include_trash: includeTrash })
      if (!alive || currentLifecycle !== lifecycle) return
      submittedScopes.set(result.id, targets)
      void task.refresh()
      if (manual) toast({ title: '正在更新资源', description: '保留当前清单，读取完成后自动更新。' })
    } catch (error: any) {
      if (!alive || currentLifecycle !== lifecycle) return
      updatingProjects.value = new Set([...updatingProjects.value].filter(id => !targets.includes(id)))
      toast({ title: '扫描未能启动', description: error?.message || '请稍后重试', variant: 'destructive' })
    }
  }

  async function loadOverview(scanMissing = false) {
    if (!alive) return
    const sequence = ++overviewSequence
    const currentLifecycle = lifecycle
    overviewAbort?.abort()
    overviewAbort = new AbortController()
    refreshing.value = true
    pageError.value = ''
    try {
      const result = await getResourceOverview({ signal: overviewAbort.signal }, tab.value === 'storage' ? undefined : { page: projectPage.value, page_size: 12, q: projectSearch.value, sort: projectSort.value, project_id: projectId.value || undefined })
      if (!alive || currentLifecycle !== lifecycle || sequence !== overviewSequence) return
      overview.value = result
      if (result.pagination && projectPage.value > Math.max(1, Math.ceil(result.pagination.total / 12))) {
        projectPage.value = Math.max(1, Math.ceil(result.pagination.total / 12)); void loadOverview()
      }
      const running = new Set(result.projects.filter(item => item.scan_task_id).map(item => item.project_id))
      updatingProjects.value = new Set([...updatingProjects.value].filter(id => running.has(id) || pendingRefresh.has(id)))
      if (scanMissing) {
        const missing = result.projects.filter(item => item.stale && !item.scan_task_id && !scanAttempts.has(item.project_id) && !item.scan_error).map(item => item.project_id)
        if (missing.length) void scanProjects(missing, false, true)
      }
    } catch (error: any) {
      if (alive && currentLifecycle === lifecycle && sequence === overviewSequence && error?.name !== 'AbortError') pageError.value = error?.message || '资源读取失败'
    } finally {
      if (alive && currentLifecycle === lifecycle && sequence === overviewSequence) { loading.value = false; refreshing.value = false }
    }
  }

  function scheduleRefresh(ids: string[]) {
    ids.forEach(id => pendingRefresh.add(id))
    updatingProjects.value = new Set([...updatingProjects.value, ...ids])
    if (refreshTimer) clearTimeout(refreshTimer)
    refreshTimer = setTimeout(() => {
      const targets = [...pendingRefresh]
      pendingRefresh.clear()
      if (alive) void scanProjects(targets)
    }, 650)
  }

  function tasksForProject(id: string): TaskSnapshot[] { return activeTasks.value.filter(item => item.project_id === id) }
  async function refreshResources(id?: string) {
    const currentLifecycle = lifecycle
    try {
      const ids = id ? [id] : (await getResourceProjectIds()).project_ids
      if (alive && lifecycle === currentLifecycle) await scanProjects(ids, true, !id)
    } catch (e: any) { if (alive && lifecycle === currentLifecycle) pageError.value = e.message }
  }
  async function enterProduction(id: string, module?: string) {
    if (busyProjectId.value) return
    busyProjectId.value = id
    try {
      await project.select(id)
      const next = module && RESOURCE_STAGES[module] ? RESOURCE_STAGES[module] : `/projects/${encodeURIComponent(id)}`
      await router.push(next)
    } catch (error: any) {
      toast({ title: '暂时无法进入制作', description: error?.message, variant: 'destructive' })
    } finally { busyProjectId.value = '' }
  }
  async function trashProject(item: ResourceProject) {
    if (!await showConfirm(`「${item.name}」将移入项目回收站，保留一个自然月。文件不会立即删除或释放磁盘空间。`, { title: '移入项目回收站', destructive: true })) return
    try {
      await deleteProject(item.project_id)
      await project.refresh()
      if (projectId.value === item.project_id) backToProjects()
      await loadOverview()
      toast({ title: '项目已移入回收站' })
    } catch (error: any) { toast({ title: '项目未能移入回收站', description: error?.message, variant: 'destructive' }) }
  }

  async function prepareExport(all: boolean) {
    if (tab.value !== 'projects') return
    if (!entries.value || entriesLoading.value || submitting.value) return
    if (all && !entries.value.complete) { toast({ title: '资源清单读取不完整', description: '请先刷新清单，或明确选择已读取的文件。', variant: 'destructive' }); return }
    const currentLifecycle = lifecycle
    const sequence = ++exportSequence
    const frozenQuery = { ...fileQuery.value, directory_mode: false }
    const queryVersion = JSON.stringify(fileQuery.value)
    let matched = entries.value
    // A directory listing contains folders and direct children. Its export
    // explicitly includes descendants, so obtain the recursive FILE count.
    if (all && directoryMode.value) {
      try {
        matched = await getResourceEntries(frozenQuery)
      } catch (error: any) {
        if (alive && currentLifecycle === lifecycle) toast({ title: '读取导出范围失败', description: error?.message, variant: 'destructive' })
        return
      }
      if (!alive || currentLifecycle !== lifecycle || sequence !== exportSequence || JSON.stringify(fileQuery.value) !== queryVersion) return
      if (!matched.complete) return
    }
    exportAll.value = all
    exportSelection.value = all ? [] : selectedFiles.value.slice()
    exportScope.value = frozenQuery
    exportReferences.value = matched.snapshots.map(item => ({ ...item }))
    exportCount.value = all ? matched.total : exportSelection.value.length
    exportBytes.value = all ? matched.size_bytes : exportSelection.value.reduce((sum, item) => sum + item.size_bytes, 0)
    if (!exportCount.value) return
    modal.value = 'export'
  }
  async function prepareProjectExport(item: ResourceProject) {
    if (!item.snapshot?.complete) return
    const currentLifecycle = lifecycle
    const sequence = ++exportSequence
    const scope: ResourceQuery = { project_id: item.project_id, category: 'deliverables', role: 'deliverable' }
    try {
      const result = await getResourceEntries(scope)
      if (!alive || currentLifecycle !== lifecycle || sequence !== exportSequence) return
      exportAll.value = true
      exportSelection.value = []
      exportScope.value = scope
      exportReferences.value = result.snapshots
      exportCount.value = result.total
      exportBytes.value = result.size_bytes
      if (result.total && result.complete) modal.value = 'export'
    } catch (error: any) { if (alive && currentLifecycle === lifecycle) toast({ title: '读取导出范围失败', description: error?.message, variant: 'destructive' }) }
  }
  async function confirmExport() {
    if (submitting.value) return
    const currentLifecycle = lifecycle
    submitting.value = true
    try {
      const payload = exportAll.value ? { scope: { ...exportScope.value, snapshots: exportReferences.value } } : { files: exportSelection.value.map(item => ({ resource_id: item.id, snapshot_id: item.snapshot_id })) }
      const anchor = exportAll.value ? exportReferences.value[0]?.project_id : exportSelection.value[0]?.project_id
      if (!anchor) return
      const result = await submitResourceTask(anchor, 'resources.package', { ...payload, name: currentProject.value?.name || '项目资源' })
      if (!alive || currentLifecycle !== lifecycle) return
      submittedScopes.set(result.id, exportReferences.value.map(item => item.project_id))
      recentOperationIds.value = new Set([...recentOperationIds.value, result.id])
      modal.value = null
      actionResult.value = `已提交${exportCount.value}个文件的打包任务，完成后在此下载。`
      void task.refresh()
    } catch (error: any) { if (alive && currentLifecycle === lifecycle) toast({ title: '打包未能提交', description: error?.message, variant: 'destructive' }) }
    finally { if (currentLifecycle === lifecycle) submitting.value = false }
  }
  async function loadCleanup(pageNumber = 1) {
    if (!cleanupIds.value.length) return
    const currentLifecycle = lifecycle
    const sequence = ++cleanupSequence
    cleanupAbort?.abort()
    cleanupAbort = new AbortController()
    cleanupLoading.value = true
    try {
      const result = await getCleanupPreview(cleanupIds.value, pageNumber, { signal: cleanupAbort.signal })
      if (!alive || currentLifecycle !== lifecycle || sequence !== cleanupSequence) return
      cleanup.value = result
      cleanupPage.value = pageNumber
    } catch (error: any) { if (alive && currentLifecycle === lifecycle && sequence === cleanupSequence && error?.name !== 'AbortError') toast({ title: '清理明细读取失败', description: error?.message, variant: 'destructive' }) }
    finally { if (currentLifecycle === lifecycle && sequence === cleanupSequence) cleanupLoading.value = false }
  }
  async function prepareCleanup(ids: string[]) {
    const currentLifecycle = lifecycle
    if (tab.value === 'storage') ids = (await getResourceProjectIds()).project_ids
    if (!alive || lifecycle !== currentLifecycle) return
    cleanupIds.value = ids
    cleanup.value = null
    modal.value = 'cleanup'
    await loadCleanup()
  }
  async function confirmCleanup() {
    if (submitting.value || !cleanup.value) return
    const currentLifecycle = lifecycle
    const refs = cleanup.value.projects.filter(item => item.count && !item.blocked && item.snapshot_id).map(item => ({ project_id: item.project_id, snapshot_id: item.snapshot_id! }))
    if (!refs.length) return
    submitting.value = true
    try {
      const result = await submitResourceTask(refs[0].project_id, 'resources.cleanup', { snapshots: refs })
      if (!alive || currentLifecycle !== lifecycle) return
      submittedScopes.set(result.id, refs.map(item => item.project_id))
      recentOperationIds.value = new Set([...recentOperationIds.value, result.id])
      modal.value = null
      actionResult.value = '已提交清理任务。执行时会再次检查项目任务和文件资格。'
      void task.refresh()
    } catch (error: any) { if (alive && currentLifecycle === lifecycle) toast({ title: '清理未能提交', description: error?.message, variant: 'destructive' }) }
    finally { if (currentLifecycle === lifecycle) submitting.value = false }
  }

  watch([projectSearch, projectSort], () => { projectPage.value = 1; void loadOverview() })
  watch(projectPage, () => { void loadOverview() })
  watch(fileSearch, (value) => {
    if (value === String(route.query.q || '')) return
    if (searchTimer) clearTimeout(searchTimer)
    searchTimer = setTimeout(() => { if (alive) void changeQuery({ q: value.trim(), page: undefined }) }, 300)
  })
  watch(fileQuery, () => {
    fileSearch.value = String(route.query.q || '')
    selection.value = new Set()
    entries.value = null
    void loadEntries()
  })
  watch([projectId, tab], () => {
    previewEntry.value = null
    modal.value = null
    void loadOverview()
    if (tab.value !== 'storage') void loadEntries()
  })
  watch(() => task.tasks.map(item => `${item.id}:${item.status}:${item.finished}`).join('|'), () => {
    if (!alive) return
    for (const item of task.tasks) {
      const previous = seenTasks.get(item.id)
      seenTasks.set(item.id, item.status)
      const created = item.created_at ? Date.parse(/[zZ]|[+-]\d\d:\d\d$/.test(item.created_at) ? item.created_at : `${item.created_at}Z`) : item.created * 1000
      const arrivedDuringSession = Number.isFinite(created) && created >= openedAt
      if (isActiveResourceTask(item) || (!previous && !submittedScopes.has(item.id) && !arrivedDuringSession) || previous === item.status) continue
      const affected = submittedScopes.get(item.id) || (item.result?.snapshots as SnapshotReference[] | undefined)?.map(ref => ref.project_id) || (item.result?.projects as SnapshotReference[] | undefined)?.map(ref => ref.project_id) || [item.project_id]
      if (item.task_type === 'resources.scan') {
        affected.forEach(id => updatingProjects.value.delete(id))
        updatingProjects.value = new Set(updatingProjects.value)
        if (item.status === 'succeeded') affected.forEach(id => scanAttempts.delete(id))
        const currentLifecycle = lifecycle
        void loadOverview(item.status === 'succeeded').then(() => loadEntries()).then(() => {
          if (!alive || currentLifecycle !== lifecycle || item.status !== 'succeeded' || pageError.value) return
          if (actionResult.value === '成品已生成，正在更新成品清单。') actionResult.value = '成品清单已更新，可试听或下载。'
          else if (actionResult.value === '合成完成，可继续合并。正在更新制作资料。') actionResult.value = '合成完成，制作资料已更新，可继续合并。'
          else if (actionResult.value.endsWith('正在更新容量。')) actionResult.value = actionResult.value.replace('正在更新容量。', '容量已更新。')
        })
      } else if (item.task_type === 'resources.package') {
        void loadOverview()
      } else {
        scheduleRefresh(affected)
      }
      if (item.task_type === 'resources.cleanup' && item.status === 'succeeded') {
        const rows = item.result?.projects || []
        const deleted = rows.reduce((sum: number, row: any) => sum + row.deleted_count, 0)
        const bytes = rows.reduce((sum: number, row: any) => sum + row.deleted_bytes, 0)
        const skipped = rows.reduce((sum: number, row: any) => sum + row.skipped_count, 0)
        const failed = rows.reduce((sum: number, row: any) => sum + row.failed_count, 0)
        const blocked = rows.filter((row: any) => row.blocked).length
        actionResult.value = `清理结果：删除${deleted}项（${resourceBytes(bytes)}），跳过${skipped}项，失败${failed}项，${blocked}个项目被任务阻塞。正在更新容量。`
      } else if (item.status === 'succeeded' && item.task_type === 'tts.batch') {
        actionResult.value = '合成完成，可继续合并。正在更新制作资料。'
      } else if (item.status === 'succeeded' && ['tts.merge', 'bgm.mix'].includes(item.task_type)) {
        actionResult.value = item.result?.deliveries?.length ? '成品已生成，正在更新成品清单。' : '此制作阶段已完成，正在核对成品与更新资源。'
      }
      submittedScopes.delete(item.id)
    }
  })
  watch(() => task.snapshotVersion, () => { if (alive) { scanAttempts.clear(); void loadOverview(true).then(() => loadEntries()) } })

  function activate() {
    if (alive) return
    alive = true
    openedAt = Date.now()
    lifecycle += 1
    releaseScope = task.acquireGlobalScope()
    void loadOverview(true).then(() => loadEntries())
  }
  function deactivate() {
    alive = false
    lifecycle += 1
    overviewAbort?.abort()
    entriesAbort?.abort()
    cleanupAbort?.abort()
    releaseScope?.()
    releaseScope = null
    if (searchTimer) clearTimeout(searchTimer)
    if (refreshTimer) clearTimeout(refreshTimer)
    previewEntry.value = null
    modal.value = null
    submitting.value = false
    seenTasks.clear()
    scanAttempts.clear()
  }
  onActivated(activate)
  onDeactivated(deactivate)
  onBeforeUnmount(deactivate)
  // Also works outside the application's keep-alive layout in component tests.
  activate()

  return {
    projectPage, overview, entries, loading, refreshing, entriesLoading, pageError, entriesError,
    projectSearch, projectSort, fileSearch, selection, previewEntry, modal, submitting,
    tab, projectId, browsing, category, path, directoryMode, extension, sort, page,
    currentProject, projects, completeCount, scanTasks, currentTasks, operationTasks,
    task, project, tasksForProject, updatingProjects, actionResult, tasksExpanded, busyProjectId,
    exportSelection, exportAll, exportCount, exportBytes, exportScope, cleanup, cleanupIds,
    cleanupPage, cleanupLoading, selectedCleanup, selectedFiles, allPageSelected,
    setTab, changeQuery, openProject, openGlobalFiles, backToProjects, selectCategory,
    enterDirectory, browseCategoryDirectory, setPage, toggleSelected, togglePageSelected,
    loadOverview, loadEntries, refreshResources, enterProduction, trashProject,
    prepareExport, prepareProjectExport, confirmExport, prepareCleanup, loadCleanup, confirmCleanup,
    entryIsSystem,
  }
}
