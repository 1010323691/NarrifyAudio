import { http } from './client'

export interface DurableTaskResult {
  file_id?: string
  object_key?: string
  name?: string
  path?: string
  stats?: { chars: number; paras: number; chapters: number }
  preview?: string
  full_length?: number
  [key: string]: unknown
}

export interface DurableTask {
  id: string
  project_id: string
  task_type: string
  status: string
  progress: number
  current?: string
  error_code: string
  error_message: string
  result: DurableTaskResult | null
}

const ACTIVE_DURABLE_STATUSES = new Set(['pending', 'queued', 'running', 'cancelling', 'retrying'])

export function findActiveDurableTask(
  tasks: DurableTask[], projectId: string, taskType: string,
): DurableTask | undefined {
  if (!projectId) return undefined
  return tasks.find((task) =>
    task.project_id === projectId
    && task.task_type === taskType
    && ACTIVE_DURABLE_STATUSES.has(task.status),
  )
}

export function submitDurableTask(payload: {
  project_id: string
  task_type: string
  payload: Record<string, unknown>
  idempotency_key: string
}): Promise<DurableTask> {
  return http.post('/api/v1/tasks', payload)
}

export function getDurableTask(taskId: string, signal?: AbortSignal): Promise<DurableTask> {
  return http.get(`/api/v1/tasks/${taskId}`, { signal })
}

export function listDurableTasks(): Promise<DurableTask[]> {
  return http.get('/api/v1/tasks')
}

export function cancelDurableTask(taskId: string): Promise<DurableTask> {
  return http.post(`/api/v1/tasks/${taskId}/cancel`, {})
}

const TERMINAL_DURABLE_STATUSES = new Set(['succeeded', 'failed', 'cancelled', 'timeout'])

function interruptibleSleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', abort)
      resolve()
    }, ms)
    const abort = () => {
      window.clearTimeout(timer)
      reject(signal?.reason ?? new DOMException('Task wait cancelled', 'AbortError'))
    }
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) abort()
  })
}

/**
 * 轮询持久化任务直到终态。onTick 收到每次状态快照；
 * 返回 false 提前停止轮询（返回最后一个快照）。
 */
export async function trackDurableTask(
  taskId: string,
  onTick: (task: DurableTask) => boolean | void,
  intervalMs = 500,
  signal?: AbortSignal,
): Promise<DurableTask> {
  while (true) {
    signal?.throwIfAborted()
    const task = await getDurableTask(taskId, signal)
    signal?.throwIfAborted()
    if (onTick(task) === false) return task
    if (TERMINAL_DURABLE_STATUSES.has(task.status)) return task
    await interruptibleSleep(intervalMs, signal)
  }
}

export function waitForDurableTask(taskId: string, intervalMs = 500, signal?: AbortSignal): Promise<DurableTask> {
  return trackDurableTask(taskId, () => undefined, intervalMs, signal)
}
