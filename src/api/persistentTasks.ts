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

export function getDurableTask(taskId: string): Promise<DurableTask> {
  return http.get(`/api/v1/tasks/${taskId}`)
}

export function listDurableTasks(): Promise<DurableTask[]> {
  return http.get('/api/v1/tasks')
}

export function cancelDurableTask(taskId: string): Promise<DurableTask> {
  return http.post(`/api/v1/tasks/${taskId}/cancel`, {})
}

export async function waitForDurableTask(taskId: string, intervalMs = 500): Promise<DurableTask> {
  while (true) {
    const task = await getDurableTask(taskId)
    if (['succeeded', 'failed', 'cancelled', 'timeout'].includes(task.status)) return task
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs))
  }
}
