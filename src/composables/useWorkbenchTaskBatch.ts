import { computed, ref, watch, type Ref } from 'vue'
import { useTaskStore } from '@/stores/task'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import type { TaskSnapshot } from '@/types'

const ACTIVE = new Set(['pending', 'queued', 'retrying', 'running', 'paused', 'cancelling'])

/** A page summary only; task center and admin queue keep the independent rows. */
export function summarizeTaskBatch(rows: TaskSnapshot[], expected: number): TaskSnapshot | null {
  if (!rows.length || !expected) return null
  const active = rows.find(row => row.status === 'running' || row.status === 'cancelling')
    ?? rows.find(row => ACTIVE.has(row.status))
  const failed = rows.find(row => row.status === 'failed' || row.status === 'timeout')
  const cancelled = rows.find(row => row.status === 'cancelled')
  const representative = active ?? failed ?? cancelled ?? rows[0]!
  const result: Record<string, any> = {}
  for (const row of rows) {
    for (const [key, value] of Object.entries(row.result ?? {})) {
      if (typeof value === 'number') result[key] = (result[key] ?? 0) + value
      else if (Array.isArray(value)) result[key] = [...(result[key] ?? []), ...value]
      else result[key] = value
    }
  }
  if (Array.isArray(result.speakers)) result.speakers = [...new Set(result.speakers)]
  const terminal = rows.filter(row => !ACTIVE.has(row.status)).length
  return {
    ...representative,
    status: rows.length < expected ? 'pending' : active?.status ?? failed?.status ?? cancelled?.status ?? 'succeeded',
    progress: rows.reduce((sum, row) => sum + row.progress, 0) / expected,
    current: `${terminal}/${expected} 条任务完成 · ${representative.current || representative.label}`,
    logs: rows.flatMap(row => (row.logs ?? []).map(log => ({ ...log, msg: `[${row.label}] ${log.msg}` }))).sort((a, b) => a.t - b.t).slice(-1000),
    seg_done: rows.reduce((sum, row) => sum + (row.seg_done ?? 0), 0),
    seg_total: rows.reduce((sum, row) => sum + (row.seg_total ?? 0), 0),
    seg_chars_done: rows.reduce((sum, row) => sum + (row.seg_chars_done ?? 0), 0),
    seg_chars_total: rows.reduce((sum, row) => sum + (row.seg_chars_total ?? 0), 0),
    result,
  }
}

export function useWorkbenchTaskBatch(firstId: Ref<string | null>) {
  const store = useTaskStore()
  const auth = useAuthStore()
  const project = useProjectStore()
  const ids = ref<string[]>([])
  const snapshots = ref<Record<string, TaskSnapshot>>({})
  watch(firstId, id => {
    if (!id || !ids.value.includes(id)) {
      ids.value = id ? [id] : []
      snapshots.value = {}
    }
  }, { flush: 'sync' })
  // Retain completed rows as they age out of the live replay's newest-200 window.
  watch(() => store.projectTasks, rows => {
    for (const row of rows) {
      if (ids.value.includes(row.id)) snapshots.value[row.id] = row
    }
  }, { deep: true, immediate: true })
  watch([() => auth.user?.id, () => project.activeProjectId], () => {
    ids.value = []
    snapshots.value = {}
    firstId.value = null
  }, { flush: 'sync' })
  function track(taskIds: string[]) {
    firstId.value = taskIds[0] ?? null
    ids.value = taskIds
    const selected = new Set(taskIds)
    snapshots.value = Object.fromEntries(store.projectTasks.filter(row => selected.has(row.id)).map(row => [row.id, row]))
  }
  const rows = computed(() => {
    const live = new Map(store.projectTasks.map(row => [row.id, row]))
    return ids.value.map(id => live.get(id) ?? snapshots.value[id]).filter((row): row is TaskSnapshot => !!row)
  })
  const task = computed(() => summarizeTaskBatch(rows.value, ids.value.length))
  async function cancel() {
    const pending = ids.value.filter(id => {
      const row = rows.value.find(row => row.id === id)
      return !row || ACTIVE.has(row.status)
    })
    const failures: unknown[] = []
    for (let i = 0; i < pending.length; i += 4) {
      const responses = await Promise.allSettled(pending.slice(i, i + 4).map(id => store.control(id, 'cancel')))
      for (const response of responses) if (response.status === 'rejected') failures.push(response.reason)
    }
    if (failures.length) throw failures[0]
  }
  return { task, track, cancel }
}
