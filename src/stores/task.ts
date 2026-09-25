import { defineStore } from 'pinia'
import { ref } from 'vue'
import { controlTask, streamAllTasks } from '@/api/tasks'
import type { TaskControl, TaskSnapshot, TaskStatus } from '@/types'

const ACTIVE: TaskStatus[] = ['pending', 'running']

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
  // refresh() callers wait here for the stream's snapshot_all replay to land.
  let snapshotWaiters: Array<() => void> = []

  /** 绑定当前项目 —— 项目切换/登录时由 stores/project.ts 调用（先于 reset/refresh）。 */
  function bindProject(projectId: string | null) {
    boundProjectId = projectId
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

  /** 非终态任务（可选按 module 过滤）——供各页在页面刷新后重新挂接在途任务。 */
  function activeTasks(module?: string): TaskSnapshot[] {
    return tasks.value.filter((t) => isActive(t.status) && (!module || t.module === module))
  }

  function upsert(t: TaskSnapshot) {
    const i = tasks.value.findIndex((x) => x.id === t.id)
    if (i === -1) tasks.value.unshift(t)
    else tasks.value[i] = t
  }

  function applyEvent(id: string, e: { type: string; [k: string]: any }) {
    if (e.type === 'snapshot_all') {
      // Connect / reconnect replay: the authoritative full task list — replace
      // wholesale so live events that follow apply to the fresh objects.
      tasks.value = Array.isArray(e.tasks) ? e.tasks : []
      wakeSnapshotWaiters()
      return
    }
    const t = tasks.value.find((x) => x.id === id)
    if (!t) {
      // A late snapshot/final carries the full task — add it if unknown.
      if ((e.type === 'snapshot' || e.type === 'final') && e.task) upsert(e.task)
      if (allStream && !hasActive()) closeStream()
      return
    }
    switch (e.type) {
      case 'snapshot':
        if (e.task) tasks.value[tasks.value.indexOf(t)] = e.task
        break
      case 'progress':
        t.progress = e.progress
        if (e.current) t.current = e.current
        break
      case 'phase':
        t.phase = typeof e.phase === 'string' ? e.phase : ''
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
      case 'llm_rate':
        // Live LLM generation rate: ``cps`` is the instantaneous per-window rate (per-row 字/s
        // gauge) and ``cps10`` is the 10-second-window average (吞吐量 card). The backend
        // computes both from the actual streamed text. A later snapshot / terminal event
        // replaces the whole task (carrying both), so this self-corrects.
        t.llm_cps = typeof e.cps === 'number' ? e.cps : 0
        t.llm_cps_10s = typeof e.cps10 === 'number' ? e.cps10 : 0
        break
      case 'llm_chars':
        // Cumulative original-text chars (处理速度 numerator) + cumulative processing
        // seconds up to this chunk's completion (denominator). Both step together per
        // chunk, so the gauge updates per segment and stays stable in between (the time
        // base is frozen at this chunk's completion, reported by the backend).
        t.llm_chars = typeof e.chars === 'number' ? e.chars : 0
        t.llm_secs = typeof e.secs === 'number' ? e.secs : 0
        break
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
        if (e.task) tasks.value[tasks.value.indexOf(t)] = e.task
        else t.status = e.status
        break
      case 'final':
        if (e.task) tasks.value[tasks.value.indexOf(t)] = e.task
        break
    }
    // The stream is only worth holding while some task is live: the last task's
    // terminal event releases the tab's connection (idle tabs shouldn't occupy
    // one of the browser's ~6 per-host slots); refresh() / control() reopen it
    // when work starts.
    if (allStream && !hasActive()) closeStream()
  }

  function ensureStream() {
    if (allStream) return
    const streamGeneration = generation
    allStream = streamAllTasks(
      (e) => {
        if (streamGeneration === generation) applyEvent(String(e.task_id ?? ''), e)
      },
      () => {
        if (streamGeneration !== generation) return
        // The connection ended (server closed / abort): settle any refresh()
        // waiting on the replay, and — if work is still in flight — reopen the
        // stream; its snapshot_all self-heals the state.
        allStream = null
        wakeSnapshotWaiters()
        if (hasActive()) ensureStream()
      },
      boundProjectId,
    )
  }

  function closeStream() {
    if (allStream) {
      allStream()
      allStream = null
    }
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
      if (!allStream) {
        // The replay is the ONLY event that can wake this waiter, so the
        // stream must be opened BEFORE the wait — waiting first would burn
        // the full timeout on a cold start and resolve with tasks still
        // empty, losing the one-shot reattach (F5 / project switch).
        ensureStream()
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

  /** The v1 control POSTs are fire-and-confirm-over-SSE: the authoritative
   *  snapshot arrives as the `status` frame that follows the request, so the
   *  response body is discarded and the stream is (re)opened to receive it. */
  async function control(id: string, action: TaskControl): Promise<void> {
    const requestGeneration = generation
    await controlTask(id, action)
    if (requestGeneration !== generation) return
    ensureStream()
  }

  function reset() {
    generation += 1
    snapshotWaiters = []
    closeStream()
    tasks.value = []
    loading.value = false
  }

  refresh()

  return { tasks, loading, refresh, control, reset, bindProject, activeTasks }
})
