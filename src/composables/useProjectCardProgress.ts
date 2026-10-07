import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { getProjectProgressSummary, type OverviewSection, type ProjectProgressSummary } from '@/api/project'
import { getProjectOverviewTasks, type ProjectOverviewTasks } from '@/api/tasks'

type Section = OverviewSection | 'tasks'
type Job = { id: string; section: Section; controller?: AbortController; timer?: ReturnType<typeof setTimeout>; queued: boolean }

// Only visible cards participate. Reserve capacity for small reads while audio counts run.
export function useProjectCardProgress(projectIds: Ref<string[]>) {
  const auth = useAuthStore()
  const summaries = ref<Record<string, ProjectProgressSummary>>({})
  const tasks = ref<Record<string, ProjectOverviewTasks>>({})
  const errors = ref<Record<string, Partial<Record<Section, string>>>>({})
  let active = false
  let generation = 0
  let jobs: Job[] = []
  let queue: Job[] = []
  const identity = computed(() => JSON.stringify([auth.user?.id ?? null, projectIds.value]))
  function cancel() {
    generation++
    queue = []
    for (const job of jobs) { job.controller?.abort(); job.controller = undefined; clearTimeout(job.timer); job.queued = false }
  }
  function enqueue(job: Job) {
    if (job.queued || job.controller) return
    job.queued = true
    queue.push(job)
  }
  function pump() {
    if (!active || document.hidden || !auth.user) return
    while (jobs.filter(job => job.controller).length < 4) {
      const audioBusy = jobs.some(job => job.section === 'production' && job.controller)
      const index = queue.findIndex(job => job.section !== 'production' || !audioBusy)
      if (index < 0) break
      const job = queue.splice(index, 1)[0]!
      job.queued = false
      clearTimeout(job.timer)
      const request = new AbortController()
      job.controller = request
      const token = generation
      void (async () => {
        try {
          const result = job.section === 'tasks'
            ? await getProjectOverviewTasks(job.id, request.signal)
            : await getProjectProgressSummary(job.id, request.signal, job.section)
          if (token !== generation || request.signal.aborted) return
          if (job.section === 'tasks') tasks.value[job.id] = result as ProjectOverviewTasks
          else {
            const summary = result as ProjectProgressSummary
            summaries.value[job.id] = { ...summary, stage_completion: {
              ...summaries.value[job.id]?.stage_completion, ...summary.stage_completion,
            } }
          }
          delete errors.value[job.id]?.[job.section]
        } catch (cause: any) {
          if (token === generation && !request.signal.aborted) {
            errors.value[job.id] ??= {}
            errors.value[job.id]![job.section] = cause?.message || '暂未更新'
          }
        } finally {
          if (token === generation) {
            job.controller = undefined
            if (active && !document.hidden) job.timer = setTimeout(() => { enqueue(job); pump() }, 5000)
            pump()
          }
        }
      })()
    }
  }
  function refresh(id?: string) {
    for (const job of jobs) if ((!id || id === job.id) && !job.controller) { clearTimeout(job.timer); enqueue(job) }
    pump()
  }
  watch(identity, () => {
    cancel()
    summaries.value = {}; tasks.value = {}; errors.value = {}
    jobs = (['tasks', 'text', 'catalog', 'production'] as Section[]).flatMap(section =>
      projectIds.value.map(id => ({ id, section, queued: false })))
    for (const job of jobs) enqueue(job)
    const token = generation
    // Let the page clear its old project IDs during the same account-change flush.
    void nextTick(() => { if (token === generation) pump() })
  }, { immediate: true, flush: 'sync' })
  function activate() { if (active) return; active = true; refresh() }
  function deactivate() { active = false; cancel() }
  function visibility() { if (document.hidden) cancel(); else refresh() }
  onMounted(() => { document.addEventListener('visibilitychange', visibility); activate() })
  onActivated(activate)
  onDeactivated(deactivate)
  onUnmounted(() => { deactivate(); document.removeEventListener('visibilitychange', visibility) })
  return { summaries, tasks, errors, refresh }
}
