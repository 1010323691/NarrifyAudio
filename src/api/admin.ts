import { http } from './client'

export interface AdminUser {
  id: string
  email: string
  username: string
  display_name: string
  role: 'user' | 'admin'
  is_active: boolean
  created_at: string
}

export interface StorageSettings {
  root_path: string
  source: 'admin' | 'deployment-default'
}

export interface QuotaSettings {
  initial_units: number
  source: 'admin' | 'deployment-default'
}

export interface RegistrationSettings {
  enabled: boolean
  source: 'admin' | 'deployment-default'
}

export interface AdminTask {
  id: string
  owner_username: string
  task_type: string
  status: string
  progress: number
  error_message: string
  created_at: string
}

export interface WorkerStatus {
  worker_id: string
  status: string
  capabilities: Record<string, unknown>
  current_task_id: string | null
  last_seen_at: string
}

export interface QueueStatus {
  available: boolean
  stream: string
  length: number
  pending: number
  error?: string
}

export function listUsers(): Promise<AdminUser[]> {
  return http.get('/api/v1/admin/users')
}

export function updateUser(id: string, payload: { is_active?: boolean; role?: 'user' | 'admin' }): Promise<{ id: string; is_active: boolean; role: 'user' | 'admin' }> {
  return http.patch(`/api/v1/admin/users/${id}`, payload)
}

export function getStorageSettings(): Promise<StorageSettings> {
  return http.get('/api/v1/admin/settings/storage')
}

export function updateStorageRoot(root_path: string): Promise<StorageSettings> {
  return http.patch('/api/v1/admin/settings/storage', { root_path })
}

export function getQuotaSettings(): Promise<QuotaSettings> {
  return http.get('/api/v1/admin/settings/quota')
}

export function updateQuotaSettings(units: number): Promise<QuotaSettings> {
  return http.patch('/api/v1/admin/settings/quota', { units })
}

export function getRegistrationSettings(): Promise<RegistrationSettings> {
  return http.get('/api/v1/admin/settings/registration')
}

export function updateRegistrationSettings(enabled: boolean): Promise<RegistrationSettings> {
  return http.patch('/api/v1/admin/settings/registration', { enabled })
}

export function adjustQuota(userId: string, amount: number, idempotency_key: string, note = ''): Promise<Record<string, number | string>> {
  return http.post(`/api/v1/admin/users/${userId}/quota/adjust`, { amount, idempotency_key, note })
}

export function listTasks(): Promise<AdminTask[]> {
  return http.get('/api/v1/admin/tasks')
}

export function cancelTask(id: string): Promise<{ id: string; status: string }> {
  return http.post(`/api/v1/admin/tasks/${id}/cancel`)
}

export function listWorkers(): Promise<WorkerStatus[]> {
  return http.get('/api/v1/admin/workers')
}

export function getQueueStatus(): Promise<QueueStatus> {
  return http.get('/api/v1/admin/queue')
}
