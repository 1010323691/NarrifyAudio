import { API_BASE, http } from './client'
import type { TaskCenterItem, TaskControl } from '@/types'
import type { TaskCenterCategoryId } from '@/utils/taskCenter'

export interface TaskHistoryPage {
  items: TaskCenterItem[]
  next_cursor: string | null
}

export function listTaskHistory(cursor?: string | null): Promise<TaskHistoryPage> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''
  return http.get(`/api/v1/tasks/history${query}`)
}

/** v1 task control (cancel / retry). The shared http client attaches the CSRF
 *  token. The response's durable dict is not consumed — the authoritative
 *  snapshot arrives as a `status` frame on the stream below. */
export function controlTask(id: string, action: TaskControl): Promise<unknown> {
  return http.post(`/api/v1/tasks/${id}/${action}`)
}

export function controlTaskCategory(
  projectId: string,
  category: TaskCenterCategoryId,
  action: 'pause' | 'resume' | 'cancel',
): Promise<{ changed: number; tasks: Array<{ id: string; status: string; error_code?: string }> }> {
  return http.post('/api/v1/tasks/batch-control', { project_id: projectId, category, action })
}

// Shared EventSource plumbing. The backend emits every
// frame as a plain `data:` line (no `event:` field), so the browser delivers
// them all as `message` events — dispatch on the JSON `type` field rather than
// named event types.
function openSse(
  url: string,
  onEvent: (e: { type: string; [k: string]: any }) => void,
  onDone?: () => void,
  onConnection?: (state: 'connected' | 'reconnecting') => void,
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
  }

  function teardown() {
    es.close()
  }

  es.onmessage = handler
  es.onopen = () => onConnection?.('connected')
  // EventSource auto-reconnects on transient errors; only a fully-closed stream
  // ends the view, so a dropped connection doesn't silently stop the live feed.
  es.onerror = () => {
    onConnection?.('reconnecting')
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
 * Subscribe to the multiplexed task SSE stream (v1): ONE connection carries
 * the live events (progress + logs + status + …) of the user's tasks, each
 * frame tagged with `task_id`. Passing ``projectId`` narrows the stream to
 * that project — this console is project-scoped. The backend replays a
 * `snapshot_all` (every task) on connect, so a reconnect self-heals the full
 * state. The stream never ends on its own; the caller aborts it. Holds
 * exactly one browser connection per tab — the browser caps HTTP/1.1
 * connections per host at ~6, so one connection *per task* would be exhausted
 * by a few parallel parses.
 */
export function streamAllTasks(
  onEvent: (e: { type: string; [k: string]: any }) => void,
  onDone?: () => void,
  projectId: string | null = null,
  onConnection?: (state: 'connected' | 'reconnecting') => void,
): () => void {
  const url = projectId
    ? `${API_BASE}/api/v1/tasks/stream?project_id=${encodeURIComponent(projectId)}`
    : `${API_BASE}/api/v1/tasks/stream`
  return openSse(url, onEvent, onDone, onConnection)
}

export interface TaskCenterCounts {
  task_count: number
  succeeded_count: number
  active_count: number
  pausable_count: number
  resumable_count: number
}
export interface TaskCenterSummary {
  items: Array<{ category: TaskCenterCategoryId; task_count: number; project_count: number; active_count: number }>
}
export interface TaskCenterGroup extends TaskCenterCounts {
  project_id: string
  project_name: string
  latest: number
  latest_status: TaskCenterItem['status']
}
export interface TaskCenterPage<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}
export type TaskCenterItems = TaskCenterPage<TaskCenterItem> & { counts: TaskCenterCounts }
export type TaskCenterFilter = 'all' | 'active' | 'completed'

export function getTaskCenterSummary(signal: AbortSignal): Promise<TaskCenterSummary> {
  return http.get('/api/v1/tasks/center/summary', { signal })
}
export function getTaskCenterGroups(category: TaskCenterCategoryId, page: number, signal: AbortSignal): Promise<TaskCenterPage<TaskCenterGroup>> {
  const query = new URLSearchParams({ category, page: String(page) })
  return http.get(`/api/v1/tasks/center/groups?${query}`, { signal })
}
export function getTaskCenterItems(category: TaskCenterCategoryId, projectId: string, filter: TaskCenterFilter, page: number, signal: AbortSignal): Promise<TaskCenterItems> {
  const query = new URLSearchParams({ category, project_id: projectId, filter, page: String(page) })
  return http.get(`/api/v1/tasks/center/items?${query}`, { signal })
}
export function controlTaskCenterGroup(projectId: string, category: TaskCenterCategoryId, action: 'pause' | 'resume' | 'cancel', signal: AbortSignal): Promise<{ changed: number }> {
  return http.post('/api/v1/tasks/batch-control?compact=true', { project_id: projectId, category, action }, { signal })
}

export interface ProjectOverviewTasks {
  statuses: Array<{ task_type: string; status: string; count: number }>
  failures: Array<{ id: string; task_type: string; error_message: string }>
  failure_count: number
}
export function getProjectOverviewTasks(projectId: string, signal: AbortSignal): Promise<ProjectOverviewTasks> {
  return http.get(`/api/v1/tasks/overview?project_id=${encodeURIComponent(projectId)}`, { signal })
}
