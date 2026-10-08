import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { controlTask, controlTaskCategory, streamAllTasks } from '@/api/tasks'
import type { TaskControl, TaskSnapshot, TaskStatus } from '@/types'
import type { TaskCenterCategoryId } from '@/utils/taskCenter'
import { taskModuleKey } from '@/utils/taskTypes'

const ACTIVE: TaskStatus[] = ['pending', 'running', 'paused']

// Client-side cap on the display-only live LLM stream buffer (「流式反馈」 panel): it
// bounds the in-memory tail; snapshot / terminal events replace the whole task,
// so any drift self-corrects on the next authoritative replay.
const LLM_STREAM_CLIENT_CAP = 128 * 1024

// How long refresh() waits for the stream's first snapshot_all before giving up
// (a healthy server delivers it within one poll tick; the timeout only bounds a
// dead connection — the stream keeps retrying underneath).
const SNAPSHOT_WAIT_TIMEOUT_MS = 10_000

export const useTaskStore = defineStore('task', () => {
  const tasks = ref<TaskSnapshot[]>([])
  const loading = ref(false)
  const connectionStatus = ref<'idle' | 'connecting' | 'connected' | 'reconnecting'>('idle')
  const snapshotVersion = ref(0)
  const globalConsumers = new Set<symbol>()
  let connectionEpoch = 0
  // Rows the stream reported as superseded (a re-run of the same entry replaced
  // the terminal row, so the server no longer displays it). The store list is
  // not the task centre's only source — /history rows stay in the view — so the
  // ids are remembered separately for the merge to filter them out too.
  const supersededIds = ref(new Set<string>())

  // ONE multiplexed SSE stream for the whole app (every event carries `task_id`).
  // Browsers cap simultaneous HTTP/1.1 connections per host at ~6 and this console
  // is routinely open in several windows/tabs, so the app must hold exactly one
  // long-lived connection per tab — the old one-EventSource-per-task design was
  // exhausted by a few parallel parses and every window beyond the cap showed no
  // logs at all (the backend ran fine). The stream is held only while at least one
  // task is non-terminal (closed on the last task's terminal event so idle tabs
  // release their connection) and is (re)opened by refresh() / control() / init
  // whenever work starts.
  let allStream: (() => void) | null = null
  let generation = 0
  // The v1 stream is user-scoped, but this console is a project console: the
  // project store binds the current project so snapshot_all carries exactly its
  // tasks (binding null = the user's tasks across all projects).
  let boundProjectId: string | null = null
  let taskCenterOpen = false
  let snapshotReceived = false
  let snapshotChunks: TaskSnapshot[] = []
  // refresh() callers wait here for the stream's snapshot_all replay to land.
  let snapshotWaiters: Array<() => void> = []

  /** 绑定当前项目 —— 项目切换/登录时由 stores/project.ts 调用（先于 reset/refresh）。 */
  function bindProject(projectId: string | null) {
    if (boundProjectId !== projectId && allStream && !globalRequested()) closeStream()
    boundProjectId = projectId
  }

  function globalRequested() {
    return taskCenterOpen || globalConsumers.size > 0
  }

  /** A global read-only consumer never changes the workbench's active project. */
  function acquireGlobalScope() {
    const token = Symbol('global-task-consumer')
    const wasGlobal = globalRequested()
    globalConsumers.add(token)
    if (!wasGlobal && allStream) closeStream()
    ensureStream()
    return () => {
      if (!globalConsumers.delete(token)) return
      if (!globalRequested() && allStream) closeStream()
      if (shouldKeepStream()) ensureStream()
    }
  }

  function wakeSnapshotWaiters() {
    const waiters = snapshotWaiters
    snapshotWaiters = []
    for (const wake of waiters) wake()
  }

  function isActive(s: TaskStatus) {
    return ACTIVE.includes(s)
  }

  function hasActive() {
    return tasks.value.some((t) => isActive(t.status))
  }

  function shouldKeepStream() {
    return globalRequested() || hasActive()
  }

  const projectTasks = computed(() => boundProjectId
    ? tasks.value.filter((task) => task.project_id === boundProjectId)
    : [])

  /** 非终态任务（可选按 module 过滤）——供各页在页面刷新后重新挂接在途任务。 */
  function activeTasks(module?: string): TaskSnapshot[] {
    return projectTasks.value.filter((t) => isActive(t.status) && (!module || t.module === module))
  }

  let taskIndex = new Map<string, number>()
  function rebuildTaskIndex() { taskIndex = new Map(tasks.value.map((task, index) => [task.id, index])) }
  watch(tasks, rebuildTaskIndex, { flush: 'sync' })
  function upsert(t: TaskSnapshot) {
    const i = taskIndex.get(t.id) ?? -1
    if (i === -1) { tasks.value.unshift(t); rebuildTaskIndex() }
    else tasks.value[i] = t
  }

  function applyEvent(id: string, e: { type: string; [k: string]: any }) {
    if (e.type === 'snapshot_add_chunk') {
      const next = [...tasks.value]
      const index = new Map(next.map((task, offset) => [task.id, offset]))
      const added: TaskSnapshot[] = []
      for (const task of e.tasks as TaskSnapshot[]) {
        const offset = index.get(task.id)
        if (offset === undefined) added.push(task)
        else next[offset] = task
      }
      tasks.value = [...added, ...next]
      return
    }
    if (e.type === 'snapshot_chunk') {
      if (e.first) snapshotChunks = []
      snapshotChunks.push(...e.tasks)
      return
    }
    if (e.type === 'snapshot_end') {
      const incoming = snapshotChunks
      snapshotChunks = []
      applyEvent('', { type: 'snapshot_all', tasks: incoming })
      return
    }
    if (e.type === 'snapshot_all') {
      // The replay is the server's visible view for this stream's scope: upsert
      // its rows and drop the store rows it no longer lists — a terminal row
      // superseded by a re-run must not survive a reconnect next to its
      // replacement (the `superseded` frame only covers rows this stream
      // session itself tracked). Rows the task centre loaded from /history
      // are unaffected: they live in the view's own merge, which re-adds them
      // only while the server still lists them there.
      const incoming = Array.isArray(e.tasks) ? e.tasks : []
      tasks.value = incoming
      snapshotReceived = true
      connectionStatus.value = 'connected'
      snapshotVersion.value += 1
      wakeSnapshotWaiters()
      if (allStream && !shouldKeepStream()) closeStream()
      return
    }
    if (e.type === 'superseded') {
      // A re-run of the same entry replaced this terminal row: drop it from the
      // live state (the 完成/失败 row must not sit next to the new 进行中 row)
      // and remember the id so the task centre's history merge can drop it too.
      const index = (taskIndex.get(id) ?? -1)
      if (index !== -1) { tasks.value.splice(index, 1); rebuildTaskIndex() }
      supersededIds.value.add(id)
      if (allStream && !shouldKeepStream()) closeStream()
      return
    }
    const position = taskIndex.get(id)
    const t = position === undefined ? undefined : tasks.value[position]
    if (!t) {
      // A late snapshot/final carries the full task — add it if unknown.
      if ((e.type === 'snapshot' || e.type === 'final' || e.type === 'status') && e.task) upsert(e.task)
      if (allStream && !shouldKeepStream()) closeStream()
      return
    }
    switch (e.type) {
      case 'snapshot':
        if (e.task) tasks.value[position!] = e.task
        break
      case 'progress':
        t.progress = e.progress
        if (e.current) t.current = e.current
        break
      case 'phase':
        t.phase = typeof e.phase === 'string' ? e.phase : ''
        if (typeof e.current === 'string' && e.current) t.current = e.current
        break
      case 'log':
        // Append chronologically (oldest first) so the UI shows the newest line at the
        // bottom; the snapshot replay uses the same order. Trim the OLDEST lines first.
        t.logs.push({ level: e.level, msg: e.msg, t: e.t })
        if (t.logs.length > 1000) t.logs.splice(0, t.logs.length - 1000)
        break
      case 'llm_chunk': {
        // Append a slice of the raw LLM stream (「流式反馈」 panel). Display-only: the
        // snapshot / terminal events replace the whole task with the authoritative,
        // backend-capped buffer, so this live append self-corrects on the next snapshot.
        const buf = (t.llm_stream ?? '') + String(e.data ?? '')
        t.llm_stream = buf.length > LLM_STREAM_CLIENT_CAP ? buf.slice(-LLM_STREAM_CLIENT_CAP) : buf
        break
      }
      case 'segments':
        // 音频合成 进度指标 (已合成/总段数 · 已合成/总字数, the button-below card): the
        // backend re-sends the FULL cumulative counters on each (throttled) event, so a
        // plain overwrite always converges — the card updates per finished sub-batch with
        // no timer. Snapshots / terminal events replay the same counters, so this
        // self-corrects after a reconnect too.
        t.seg_done = typeof e.done === 'number' ? e.done : 0
        t.seg_total = typeof e.total === 'number' ? e.total : 0
        t.seg_chars_done = typeof e.chars_done === 'number' ? e.chars_done : 0
        t.seg_chars_total = typeof e.chars_total === 'number' ? e.chars_total : 0
        break
      case 'status':
        // A terminal status arrives with the full snapshot (result / error); apply it
        // atomically so the completion handler never reads a stale, empty result.
        if (e.task) tasks.value[position!] = e.task
        else t.status = e.status
        break
      case 'paused':
        t.status = 'paused'
        t.error_code = 'manual_pause'
        t.current = '用户已暂停任务'
        break
      case 'resumed':
        t.status = 'running'
        t.error_code = ''
        t.current = ''
        break
      case 'resume_requested':
        t.status = 'pending'
        t.current = '等待启动'
        break
      case 'final':
        if (e.task) tasks.value[position!] = e.task
        break
    }
    // The stream is only worth holding while some task is live: the last task's
    // terminal event releases the tab's connection (idle tabs shouldn't occupy
    // one of the browser's ~6 per-host slots); refresh() / control() reopen it
    // when work starts.
    if (allStream && !shouldKeepStream()) closeStream()
  }

  function ensureStream() {
    if (allStream) return
    const streamGeneration = generation
    const streamEpoch = connectionEpoch
    connectionStatus.value = 'connecting'
    allStream = streamAllTasks(
      (e) => {
        if (streamGeneration === generation && streamEpoch === connectionEpoch) {
          applyEvent(String(e.task_id ?? ''), e)
        }
      },
      () => {
        if (streamGeneration !== generation || streamEpoch !== connectionEpoch) return
        // The connection ended (server closed / abort): settle any refresh()
        // waiting on the replay, and — if work is still in flight — reopen the
        // stream; its snapshot_all self-heals the state.
        allStream = null
        snapshotReceived = false
        wakeSnapshotWaiters()
        if (shouldKeepStream()) ensureStream()
      },
      globalRequested() ? null : boundProjectId,
      (state) => {
        if (streamGeneration === generation && streamEpoch === connectionEpoch) connectionStatus.value = state
      },
    )
  }

  function closeStream() {
    snapshotChunks = []
    connectionEpoch += 1
    if (allStream) {
      allStream()
      allStream = null
      snapshotReceived = false
    }
    connectionStatus.value = 'idle'
  }

  /** List source of truth is the stream's `snapshot_all` replay (the v1 list
   *  endpoint returns the lean durable shape, not the UI snapshot): refresh
   *  opens the stream if needed and resolves once the replay lands (or the
   *  connection ends / the wait times out). An already-open stream is current
   *  by construction, so it resolves immediately. */
  async function refresh() {
    const requestGeneration = generation
    loading.value = true
    try {
      if (!allStream || !snapshotReceived) {
        // The replay is the ONLY event that can wake this waiter, so the
        // stream must be opened BEFORE the wait — waiting first would burn
        // the full timeout on a cold start and resolve with tasks still
        // empty, losing the one-shot reattach (F5 / project switch).
        const gate: { drop?: () => void } = {}
        const replay = new Promise<void>((resolve) => {
          const wait = () => {
            if (requestGeneration === generation) resolve()
          }
          snapshotWaiters.push(wait)
          // The timeout path settles without the replay — remove the waiter
          // then, so it never lingers in snapshotWaiters as an orphan.
          gate.drop = () => {
            const i = snapshotWaiters.indexOf(wait)
            if (i !== -1) snapshotWaiters.splice(i, 1)
          }
        })
        ensureStream()
        const timedOut = await Promise.race([
          replay,
          new Promise<true>((resolve) => setTimeout(() => resolve(true), SNAPSHOT_WAIT_TIMEOUT_MS)),
        ])
        if (timedOut) gate.drop?.()
      }
    } finally {
      if (requestGeneration === generation) loading.value = false
    }
  }

  /** Apply the immediate control response, then let SSE confirm the durable snapshot. */
  async function control(id: string, action: TaskControl): Promise<void> {
    const requestGeneration = generation
    const response = await controlTask(id, action) as {
      status?: TaskStatus
      project_id?: string
      task_type?: string
      progress?: number
      error_message?: string
      created_at?: string
    }
    if (requestGeneration !== generation) return
    const task = tasks.value.find((item) => item.id === id)
    if (response?.status) {
      const status = response.status === 'queued' || response.status === 'retrying'
        ? 'pending'
        : response.status === 'cancelling' ? 'running' : response.status
      if (task) task.status = status
      else {
        const created = response.created_at ? new Date(response.created_at).getTime() / 1000 : Date.now() / 1000
        upsert({
          id,
          project_id: response.project_id || boundProjectId || '',
          project_name: '',
          task_type: response.task_type || '',
          module: taskModuleKey(response.task_type || ''),
          label: '任务状态更新',
          status,
          progress: typeof response.progress === 'number' ? response.progress / 100 : 0,
          current: '',
          logs: [],
          result: {},
          error: response.error_message || '',
          created,
          created_at: response.created_at || new Date(created * 1000).toISOString(),
          started: 0,
          finished: 0,
          seq: created * 1000,
        })
      }
    }
    ensureStream()
  }

  async function controlCategory(
    projectId: string, category: TaskCenterCategoryId, action: 'pause' | 'resume' | 'cancel',
  ) {
    const requestGeneration = generation
    const response = await controlTaskCategory(projectId, category, action)
    if (requestGeneration !== generation) return response.changed
    for (const item of response.tasks) {
      const task = tasks.value.find((candidate) => candidate.id === item.id)
      if (!task) continue
      task.status = item.status === 'queued' || item.status === 'retrying' ? 'pending' : item.status as TaskStatus
      task.error_code = item.error_code ?? (task.status === 'paused' ? 'manual_pause' : '')
      task.current = task.status === 'paused'
        ? '用户已暂停任务'
        : task.status === 'cancelling' ? '取消中，等待任务安全停止'
          : task.status === 'cancelled' ? '任务已取消'
        : task.status === 'pending' ? '等待启动' : ''
      if (task.status !== 'paused') task.error = ''
    }
    ensureStream()
    return response.changed
  }

  function setTaskCenterOpen(open: boolean) {
    const wasGlobal = globalRequested()
    taskCenterOpen = open
    if (wasGlobal !== globalRequested() && allStream) closeStream()
    if (shouldKeepStream()) ensureStream()
    else if (allStream) closeStream()
  }

  function reset() {
    snapshotChunks = []
    generation += 1
    boundProjectId = null
    taskCenterOpen = false
    globalConsumers.clear()
    snapshotVersion.value = 0
    snapshotWaiters = []
    closeStream()
    tasks.value = []
    supersededIds.value = new Set()
    loading.value = false
  }

  return { tasks, projectTasks, loading, refresh, control, controlCategory, reset, bindProject, activeTasks, setTaskCenterOpen, supersededIds, acquireGlobalScope, connectionStatus, snapshotVersion }
})
