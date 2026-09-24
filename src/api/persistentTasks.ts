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

export function submitDurableTask(payload: {
  project_id: string
  task_type: string
  payload: Record<string, unknown>
  estimated_units: number
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

export async function waitForDurableTask(taskId: string, intervalMs = 500, signal?: AbortSignal): Promise<DurableTask> {
  while (true) {
    signal?.throwIfAborted()
    const task = await getDurableTask(taskId, signal)
    signal?.throwIfAborted()
    if (['succeeded', 'failed', 'cancelled', 'timeout'].includes(task.status)) return task
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        signal?.removeEventListener('abort', abort)
        resolve()
      }, intervalMs)
      const abort = () => {
        window.clearTimeout(timer)
        reject(signal?.reason ?? new DOMException('Task wait cancelled', 'AbortError'))
      }
      signal?.addEventListener('abort', abort, { once: true })
      if (signal?.aborted) abort()
    })
  }
}
