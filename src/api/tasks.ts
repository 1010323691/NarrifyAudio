import { API_BASE, http } from './client'
import type { TaskControl, TaskSnapshot } from '@/types'

/** List all tasks (newest first). */
export function listTasks(): Promise<TaskSnapshot[]> {
  return http.get<TaskSnapshot[]>('/api/tasks')
}

/** cancel / pause / resume / retry. */
export function controlTask(id: string, action: TaskControl): Promise<TaskSnapshot> {
  return http.post<TaskSnapshot>(`/api/tasks/${id}/${action}`)
}

// Shared EventSource plumbing for both SSE endpoints. The backend emits every
// frame as a plain `data:` line (no `event:` field), so the browser delivers
// them all as `message` events — dispatch on the JSON `type` field rather than
// named event types.
function openSse(
  url: string,
  onEvent: (e: { type: string; [k: string]: any }) => void,
  /** Per-task streams end with a `final` event; the multiplexed stream never
   * ends on its own (tasks keep being created), so no final-driven teardown. */
  endsOnFinal: boolean,
  onDone?: () => void,
): () => void {
  const es = new EventSource(url)

  let finished = false
  const handler = (ev: MessageEvent) => {
    let e: { type: string; [k: string]: any }
    try {
      e = JSON.parse(ev.data)
    } catch {
      return
    }
    onEvent(e)
    if (endsOnFinal && e.type === 'final' && !finished) {
      finished = true
      teardown()
      onDone?.()
    }
  }

  function teardown() {
    es.close()
  }

  es.onmessage = handler
  // EventSource auto-reconnects on transient errors; only a fully-closed stream
  // ends the view, so a dropped connection doesn't silently stop the live feed.
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && !finished) {
      finished = true
      teardown()
      onDone?.()
    }
  }

  return () => {
    if (!finished) {
      finished = true
      teardown()
    }
  }
}

/**
 * Subscribe to the multiplexed task SSE stream: ONE connection carries the live
 * events (progress + logs + status + …) of ALL tasks, each event tagged with
 * `task_id`. The backend replays a `snapshot_all` (every task) on connect, so a
 * reconnect self-heals the full state. This stream never ends on its own; the
 * caller aborts it. Holds exactly one browser connection per tab — the browser
 * caps HTTP/1.1 connections per host at ~6, so one connection *per task* is
 * exhausted by a few parallel parses and every EventSource beyond the cap never
 * connects (its window shows no logs while the backend runs fine).
 */
export function streamAllTasks(
  onEvent: (e: { type: string; [k: string]: any }) => void,
  onDone?: () => void,
): () => void {
  return openSse(`${API_BASE}/api/tasks/stream`, onEvent, false, onDone)
}

/**
 * Subscribe to a single task's live SSE stream (progress + logs + status). The
 * backend replays the current snapshot on connect, then forwards events until
 * the task reaches a terminal state (the `final` event). Returns an abort
 * function. Superseded by :func:`streamAllTasks` for in-app UI (connection
 * economy); kept for one-off consumers.
 */
export function streamTask(
  id: string,
  onEvent: (e: { type: string; [k: string]: any }) => void,
  onDone?: () => void,
): () => void {
  return openSse(`${API_BASE}/api/tasks/${id}/stream`, onEvent, true, onDone)
}
