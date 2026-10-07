import { computed, onActivated, onDeactivated, onMounted, onUnmounted, shallowRef, ref, watch, type Ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { getProjectProgressSummary, type ProjectProgressSummary, type OverviewSection } from '@/api/project'
import { getProjectOverviewTasks } from '@/api/tasks'

export function useProjectOverview(projectId: Ref<string>) {
  const auth = useAuthStore()
  let active = false
  const key = () => auth.user?.id && projectId.value ? JSON.stringify([auth.user.id, projectId.value]) : null
  function region<T>(label: string, fetch: (id: string, signal: AbortSignal) => Promise<T>) {
    const data = shallowRef<T | null>(null)
    const loading = ref(false)
    const error = ref('')
    let generation = 0
    let controller: AbortController | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    function cancel() { generation++; controller?.abort(); controller = null; clearTimeout(timer); loading.value = false }
    async function refresh() {
      const requestKey = key()
      if (!active || document.hidden || !requestKey || controller) return
      clearTimeout(timer)
      const token = ++generation
      const request = new AbortController()
      controller = request
      loading.value = true
      try {
        const result = await fetch(projectId.value, request.signal)
        if (token !== generation || requestKey !== key() || !active || request.signal.aborted) return
        data.value = result
        error.value = ''
      } catch (cause: any) {
        if (token === generation && !request.signal.aborted) error.value = cause?.message || '暂未更新，请重试'
      } finally {
        if (token === generation) {
          controller = null
          loading.value = false
          if (active && !document.hidden && key()) timer = setTimeout(() => void refresh(), 5000)
        }
      }
    }
    function reset() { cancel(); data.value = null; error.value = '' }
    return { label, data, loading, error, refresh, cancel, reset }
  }
  const taskRegion = region('任务状态', getProjectOverviewTasks)
  const sections = (['text', 'catalog', 'production'] as OverviewSection[]).map(section => region<ProjectProgressSummary>(
    { text: '排版与分册', catalog: '解析与角色', production: '音频制作' }[section],
    (id, signal) => getProjectProgressSummary(id, signal, section),
  ))
  const regions = [taskRegion, ...sections]
  const completion = computed(() => Object.assign({}, ...sections.map(section => section.data.value?.stage_completion ?? {})) as NonNullable<ProjectProgressSummary['stage_completion']>)
  const loading = computed(() => sections.some(section => !section.data.value))
  const refreshing = computed(() => regions.some(item => item.loading.value))
  const errors = computed(() => regions.filter(item => item.error.value).map(item => ({ label: item.label, message: item.error.value, retry: item.refresh })))
  function refresh() { for (const item of regions) void item.refresh() }
  function activate() { if (active) return; active = true; refresh() }
  function deactivate() { active = false; for (const item of regions) item.cancel() }
  function visibility() { if (document.hidden) { for (const item of regions) item.cancel() } else if (active) refresh() }
  watch(key, () => { for (const item of regions) item.reset(); if (active) refresh() }, { flush: 'sync' })
  onMounted(() => { document.addEventListener('visibilitychange', visibility); activate() })
  onActivated(activate)
  onDeactivated(deactivate)
  onUnmounted(() => { deactivate(); document.removeEventListener('visibilitychange', visibility) })
  return { completion, loading, refreshing, errors, refresh, tasks: taskRegion.data,
    tasksLoading: taskRegion.loading, tasksError: taskRegion.error }
}
