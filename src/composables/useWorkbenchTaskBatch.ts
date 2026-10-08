import { computed, ref, shallowRef, watch, type Ref } from 'vue'
import { useTaskStore } from '@/stores/task'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import type { TaskSnapshot } from '@/types'

const ACTIVE = new Set(['pending', 'queued', 'retrying', 'running', 'paused', 'cancelling'])

function summarizeResults(rows: TaskSnapshot[]) {
  const result: Record<string, any> = {}
  const speakers = new Set<string>()
  for (const row of rows) {
    for (const [key, value] of Object.entries(row.result ?? {})) {
      if (typeof value === 'number') result[key] = (result[key] ?? 0) + value
      else if (key === 'speakers' && Array.isArray(value)) {
        result.speakers = []
        for (const name of value) speakers.add(name)
      } else if (Array.isArray(value)) {
        const target = result[key] ?? (result[key] = [])
        for (const item of value) target.push(item)
      }
      else result[key] = value
    }
  }
  if (result.speakers) result.speakers = [...speakers]
  return result
}

function summarizeLogs(rows: TaskSnapshot[]) {
  // Each task's log is chronological. Merge only the newest 1000 entries,
  // rather than flattening and sorting every historical log in the batch.
  type Cursor = { row: TaskSnapshot; rowIndex: number; index: number }
  const heap: Cursor[] = []
  const newer = (a: Cursor, b: Cursor) => {
    const delta = a.row.logs[a.index]!.t - b.row.logs[b.index]!.t
    return delta > 0 || (delta === 0 && (a.rowIndex > b.rowIndex || (a.rowIndex === b.rowIndex && a.index > b.index)))
  }
  function push(cursor: Cursor) {
    heap.push(cursor)
    let index = heap.length - 1
    while (index > 0) {
      const parent = (index - 1) >> 1
      if (!newer(heap[index]!, heap[parent]!)) break
      ;[heap[index], heap[parent]] = [heap[parent]!, heap[index]!]
      index = parent
    }
  }
  function pop(): Cursor {
    const first = heap[0]!
    const last = heap.pop()!
    if (heap.length) {
      heap[0] = last
      let index = 0
      while (index * 2 + 1 < heap.length) {
        let next = index * 2 + 1
        if (next + 1 < heap.length && newer(heap[next + 1]!, heap[next]!)) next++
        if (!newer(heap[next]!, heap[index]!)) break
        ;[heap[index], heap[next]] = [heap[next]!, heap[index]!]
        index = next
      }
    }
    return first
  }
  rows.forEach((row, rowIndex) => {
    if (row.logs?.length) push({ row, rowIndex, index: row.logs.length - 1 })
  })
  const tail = []
  while (heap.length && tail.length < 1000) {
    const cursor = pop()
    const log = cursor.row.logs[cursor.index]!
    tail.push({ level: log.level, t: log.t, msg: `[${cursor.row.label}] ${log.msg}` })
    if (cursor.index) push({ ...cursor, index: cursor.index - 1 })
  }
  return tail.reverse()
}

/** A page summary only; details are evaluated separately by the live composable. */
export function summarizeTaskBatch(rows: TaskSnapshot[], expected: number, details = true): TaskSnapshot | null {
  if (!rows.length || !expected) return null
  const active = rows.find(row => row.status === 'running' || row.status === 'cancelling')
    ?? rows.find(row => ACTIVE.has(row.status))
  const failed = rows.find(row => row.status === 'failed' || row.status === 'timeout')
  const cancelled = rows.find(row => row.status === 'cancelled')
  const representative = active ?? failed ?? cancelled ?? rows[0]!
  const terminal = rows.filter(row => !ACTIVE.has(row.status)).length
  return {
    ...representative,
    batch_task_count: expected,
    status: rows.length < expected ? 'pending' : active?.status ?? failed?.status ?? cancelled?.status ?? 'succeeded',
    progress: rows.reduce((sum, row) => sum + row.progress, 0) / expected,
    current: `${terminal}/${expected} 条任务完成 · ${representative.current || representative.label}`,
    logs: details ? summarizeLogs(rows) : [],
    seg_done: rows.reduce((sum, row) => sum + (row.seg_done ?? 0), 0),
    seg_total: rows.reduce((sum, row) => sum + (row.seg_total ?? 0), 0),
    seg_chars_done: rows.reduce((sum, row) => sum + (row.seg_chars_done ?? 0), 0),
    seg_chars_total: rows.reduce((sum, row) => sum + (row.seg_chars_total ?? 0), 0),
    result: details ? summarizeResults(rows) : {},
  }
}

export function useWorkbenchTaskBatch(firstId: Ref<string | null>) {
  const store = useTaskStore()
  const auth = useAuthStore()
  const project = useProjectStore()
  const ids = ref<string[]>([])
  const snapshots = shallowRef<Record<string, TaskSnapshot>>({})
  watch(firstId, id => {
    if (!id || !ids.value.includes(id)) {
      ids.value = id ? [id] : []
      snapshots.value = {}
    }
  }, { flush: 'sync' })
  // Retain completed rows as they age out of the live replay's newest-200 window.
  watch(() => store.projectTasks.map(row => row), rows => {
    const selected = new Set(ids.value)
    for (const row of rows) {
      if (selected.has(row.id)) snapshots.value[row.id] = row
    }
  }, { immediate: true })
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
  // Status/progress events must not rebuild every completed result and log line.
  // Getters keep these independent computeds lazy, including when logs are hidden.
  const result = computed(() => summarizeResults(rows.value))
  const logs = computed(() => summarizeLogs(rows.value))
  const task = computed(() => {
    const summary = summarizeTaskBatch(rows.value, ids.value.length, false)
    if (!summary) return null
    return Object.defineProperties(summary, {
      result: { enumerable: true, get: () => result.value },
      logs: { enumerable: true, get: () => logs.value },
    })
  })
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
  return { task, rows, track, cancel }
}
