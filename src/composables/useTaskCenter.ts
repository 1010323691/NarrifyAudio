import { computed, onActivated, onBeforeUnmount, onDeactivated, ref, shallowRef, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import {
  controlTaskCenterGroup, getTaskCenterGroups, getTaskCenterItems, getTaskCenterSummary,
  type TaskCenterFilter, type TaskCenterGroup, type TaskCenterItems, type TaskCenterPage, type TaskCenterSummary,
} from '@/api/tasks'
import { TASK_CENTER_CATEGORIES, type TaskCenterCategoryId } from '@/utils/taskCenter'

const REFRESH_MS = 5000
const CACHE_LIMIT = 24

export function useTaskCenter() {
  const auth = useAuthStore()
  const category = ref<TaskCenterCategoryId | null>(null)
  const groupPage = ref(1)
  const selectedGroup = ref<{ categoryId: TaskCenterCategoryId; projectId: string; initial: TaskCenterGroup } | null>(null)
  const taskFilter = ref<TaskCenterFilter>('all')
  const taskPage = ref(1)
  const controllingCategory = ref<TaskCenterCategoryId | null>(null)
  const controlError = ref('')
  let active = false
  let mounted = false
  let controlEpoch = 0
  let controlController: AbortController | null = null

  function slot<T>(key: () => string | null, fetch: (signal: AbortSignal) => Promise<T>, apply?: (data: T) => void) {
    const data = shallowRef<T | null>(null)
    const loading = ref(false)
    const error = ref('')
    const cache = new Map<string, T>()
    let epoch = 0
    let controller: AbortController | null = null
    let timer: ReturnType<typeof setTimeout> | undefined

    function cancel() {
      epoch++
      controller?.abort()
      controller = null
      clearTimeout(timer)
      timer = undefined
      loading.value = false
    }
    async function refresh() {
      const requestKey = key()
      if (!active || document.hidden || !requestKey || controller) return
      clearTimeout(timer)
      const requestEpoch = ++epoch
      const requestController = new AbortController()
      controller = requestController
      loading.value = true
      error.value = ''
      try {
        const result = await fetch(requestController.signal)
        if (requestEpoch !== epoch || requestKey !== key() || !active) return
        cache.delete(requestKey)
        cache.set(requestKey, result)
        if (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value!)
        data.value = result
        apply?.(result)
      } catch (cause: any) {
        if (requestEpoch === epoch && !requestController.signal.aborted) {
          error.value = cause?.message || '暂未更新，请重试'
        }
      } finally {
        if (requestEpoch === epoch) {
          controller = null
          loading.value = false
          if (active && !document.hidden && key()) timer = setTimeout(() => { void refresh() }, REFRESH_MS)
        }
      }
    }
    function select() {
      cancel()
      data.value = cache.get(key() ?? '') ?? null
      error.value = ''
      void refresh()
    }
    function reset() { cancel(); cache.clear(); data.value = null; error.value = '' }
    return { data, loading, error, refresh, cancel, select, reset }
  }

  const userKey = () => auth.user?.id ?? null
  const summarySlot = slot<TaskCenterSummary>(userKey, getTaskCenterSummary, (value) => {
    if (!category.value) {
      category.value = value.items.find(item => item.active_count > 0)?.category
        ?? value.items.find(item => item.task_count > 0)?.category
        ?? TASK_CENTER_CATEGORIES[0].id
    }
  })
  const groupsSlot = slot<TaskCenterPage<TaskCenterGroup>>(
    () => userKey() && category.value ? JSON.stringify([userKey(), category.value, groupPage.value]) : null,
    signal => getTaskCenterGroups(category.value!, groupPage.value, signal),
    value => {
      const last = Math.max(1, Math.ceil(value.total / value.page_size))
      if (groupPage.value > last) groupPage.value = last
    },
  )
  const itemsSlot = slot<TaskCenterItems>(
    () => userKey() && selectedGroup.value
      ? JSON.stringify([userKey(), selectedGroup.value.categoryId, selectedGroup.value.projectId, taskFilter.value, taskPage.value]) : null,
    signal => getTaskCenterItems(selectedGroup.value!.categoryId, selectedGroup.value!.projectId, taskFilter.value, taskPage.value, signal),
    value => {
      const last = Math.max(1, Math.ceil(value.total / value.page_size))
      if (taskPage.value > last) taskPage.value = last
    },
  )
  const slots = [summarySlot, groupsSlot, itemsSlot]

  function invalidateControl() {
    controlEpoch++
    controlController?.abort()
    controlController = null
    controllingCategory.value = null
    controlError.value = ''
  }
  watch(() => [category.value, groupPage.value], () => groupsSlot.select(), { flush: 'sync' })
  watch(() => [selectedGroup.value?.categoryId, selectedGroup.value?.projectId, taskFilter.value, taskPage.value], () => itemsSlot.select(), { flush: 'sync' })
  watch(() => auth.user?.id, () => {
    const wasActive = active
    active = false
    invalidateControl()
    slots.forEach(value => value.reset())
    selectedGroup.value = null
    category.value = null
    groupPage.value = taskPage.value = 1
    taskFilter.value = 'all'
    active = wasActive
    if (active) void summarySlot.refresh()
  }, { flush: 'sync' })

  function selectCategory(value: TaskCenterCategoryId) {
    // Suppress intermediate requests when both the category and page change.
    const wasActive = active
    active = false
    groupPage.value = 1
    category.value = value
    active = wasActive
    groupsSlot.select()
  }
  function openGroup(value: TaskCenterGroup) {
    const wasActive = active
    active = false
    invalidateControl()
    taskPage.value = 1
    taskFilter.value = 'all'
    selectedGroup.value = { categoryId: category.value!, projectId: value.project_id, initial: value }
    active = wasActive
    itemsSlot.select()
  }
  function closeGroup() {
    invalidateControl()
    selectedGroup.value = null
    itemsSlot.reset()
  }
  function selectFilter(value: TaskCenterFilter) {
    const wasActive = active
    active = false
    taskPage.value = 1
    taskFilter.value = value
    active = wasActive
    itemsSlot.select()
  }
  function refreshAll() { slots.forEach(value => { void value.refresh() }) }
  async function control(action: 'pause' | 'resume' | 'cancel') {
    if (!selectedGroup.value || controllingCategory.value) return
    const { categoryId, projectId } = selectedGroup.value
    const requestEpoch = ++controlEpoch
    controllingCategory.value = categoryId
    controlError.value = ''
    controlController = new AbortController()
    try {
      await controlTaskCenterGroup(projectId, categoryId, action, controlController.signal)
      if (requestEpoch !== controlEpoch) return
      // Abort pre-operation reads so a stale response cannot undo the refresh.
      slots.forEach(value => value.cancel())
      refreshAll()
    } catch (cause: any) {
      if (requestEpoch === controlEpoch) controlError.value = cause?.message || '批量操作失败，请重试'
    } finally {
      if (requestEpoch === controlEpoch) { controllingCategory.value = null; controlController = null }
    }
  }

  function suspend() {
    active = false
    slots.forEach(value => value.cancel())
    invalidateControl()
  }
  function visibilityChanged() {
    if (document.hidden) suspend()
    else { active = mounted; if (active) refreshAll() }
  }
  onActivated(() => {
    mounted = true
    document.addEventListener('visibilitychange', visibilityChanged)
    active = !document.hidden
    refreshAll()
  })
  onDeactivated(() => {
    mounted = false
    suspend()
    closeGroup()
    document.removeEventListener('visibilitychange', visibilityChanged)
  })
  onBeforeUnmount(() => {
    mounted = false
    suspend()
    slots.forEach(value => value.reset())
    document.removeEventListener('visibilitychange', visibilityChanged)
  })

  const sections = computed(() => TASK_CENTER_CATEGORIES.map(value => ({
    ...value,
    ...(summarySlot.data.value?.items.find(item => item.category === value.id)
      ?? { task_count: 0, project_count: 0, active_count: 0 }),
  })))
  const browsingSection = computed(() => sections.value.find(value => value.id === category.value) ?? sections.value[0]!)
  const selectedCounts = computed(() => itemsSlot.data.value?.counts ?? selectedGroup.value?.initial)
  return {
    sections, browsingSection, groupPage, selectedGroup, taskFilter, taskPage, selectedCounts,
    summary: summarySlot.data, summaryLoading: summarySlot.loading, summaryError: summarySlot.error,
    groups: groupsSlot.data, groupsLoading: groupsSlot.loading, groupsError: groupsSlot.error,
    items: itemsSlot.data, itemsLoading: itemsSlot.loading, itemsError: itemsSlot.error,
    controllingCategory, controlError, selectCategory, openGroup, closeGroup, selectFilter, control, refreshAll,
    refreshSummary: summarySlot.refresh, refreshGroups: groupsSlot.refresh, refreshItems: itemsSlot.refresh,
  }
}
