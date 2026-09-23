import { http } from './client'

export interface AdminUser {
  id: string
  email: string
  username: string
  display_name: string
  role: 'user' | 'admin'
  is_active: boolean
  created_at: string
  last_seen_at?: string | null
  project_count?: number
  workspace_count?: number
  workspace_file_count?: number
  storage_bytes?: number
  file_count?: number
  file_bytes?: number
  available_units?: number
  reserved_units?: number
  consumed_units?: number
}

export interface ServiceStatus { key: string; name: string; status: 'healthy' | 'warning' | 'error' | 'unknown'; detail: string }
export interface GpuStatus { index: number; name: string; utilization_percent: number | null; memory_used_mb: number | null; memory_total_mb: number | null; temperature_c: number | null; power_w?: number | null }
export interface AdminEvent { id: string; time: string; level: string; module: string; type: string; message: string }
export interface AdminOverview {
  generated_at: string; services: ServiceStatus[]; today: { completed: number; failed: number; active_users: number; api_requests: number; llm_tokens: number | null; tts_characters: number | null; api_requests_scope: string }
  tasks: { running: number; queued: number; failed: number }; workers: TaskMetrics['worker_pool']; queue: QueueStatus; api: ApiSnapshot
  system: { cpu_percent: number | null; ram_used_bytes: number | null; ram_total_bytes: number | null }
  gpu: GpuStatus[]; user_count: number; recent_errors: AdminEvent[]
}
export interface AdminPerformance {
  generated_at: string
  system: { disk_total_bytes: number; disk_used_bytes: number; disk_free_bytes: number; cpu_percent: number | null; ram_used_bytes: number | null; ram_total_bytes: number | null; uptime_seconds: number | null; load_average_1m?: number }
  gpu: GpuStatus[]; tasks: TaskMetrics; workers: WorkerStatus[]; queue: QueueStatus; api: ApiSnapshot; unavailable_metrics: string[]
}
export interface ApiSnapshot {
  window_seconds: number; request_count: number; server_error_count: number; error_rate: number
  average_ms: number | null; p95_ms: number | null; status_counts: Record<string, number>; scope: string
  endpoints: { route: string; requests: number; server_errors: number; error_rate: number; average_ms: number; p95_ms: number }[]
}
export interface AdminResources {
  root_path: string; disk_total_bytes: number; disk_used_bytes: number; disk_free_bytes: number
  workspaces: number; projects: number; files: { kind: string; count: number; size_bytes: number }[]
  users: { username: string; workspace_count?: number; file_count?: number; count?: number; size_bytes: number; registered_file_count?: number; registered_file_bytes?: number }[]
  workspace_storage?: {
    size_bytes?: number; file_count?: number
    categories?: { kind: string; label: string; count: number; size_bytes: number }[]
    cleanup_candidates?: { count: number; size_bytes: number; older_than_days: number }
  }
  scope: string
  music_library: { count: number; size_bytes: number; assigned_chapters?: number }
}

export interface RuntimeSettings {
  limits: { max_upload_bytes: number; session_ttl_hours: number; task_lease_seconds: number; task_max_attempts: number }
  source: 'deployment-environment'
  editable_in_console: false
  model_settings_scope: 'platform'
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
  error_code?: string
  created_at: string
  updated_at: string
  project_id?: string
  started_at?: string | null
  finished_at?: string | null
  worker_id?: string | null
  attempt_no?: number
}

export interface TaskTypeMetric {
  task_type: string
  total: number
  statuses: Record<string, number>
}

export interface TaskMetrics {
  total: number
  status_counts: Record<string, number>
  stage_counts: {
    production: number
    queued: number
    consuming: number
    completed: number
    attention: number
  }
  throughput_60s: { window_seconds: number; submitted: number; started: number; completed: number; failed: number }
  worker_pool: { online_workers: number; total_slots: number; active_slots: number; idle_slots: number }
  by_type: TaskTypeMetric[]
  generated_at: string
}

export interface TaskActivity {
  task_id: string
  task_type: string
  owner_username: string
  status: string
  progress: number
  event_type: string
  payload: Record<string, unknown>
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

export function getRuntimeSettings(): Promise<RuntimeSettings> {
  return http.get('/api/v1/admin/settings/runtime')
}

export function getApplicationSettings(): Promise<{ config: import('@/types').AppConfig; source: 'admin' | 'deployment-default' }> {
  return http.get('/api/v1/admin/settings/application')
}

export function updateApplicationSettings(config: Record<string, unknown>): Promise<{ config: import('@/types').AppConfig; source: 'admin' }> {
  return http.patch('/api/v1/admin/settings/application', config)
}

export function updateRegistrationSettings(enabled: boolean): Promise<RegistrationSettings> {
  return http.patch('/api/v1/admin/settings/registration', { enabled })
}

export function adjustQuota(userId: string, amount: number, idempotency_key: string, note = ''): Promise<Record<string, number | string>> {
  return http.post(`/api/v1/admin/users/${userId}/quota/adjust`, { amount, idempotency_key, note })
}

export function listTasks(status = 'all', search = '', limit = 50): Promise<AdminTask[]> {
  const query = new URLSearchParams({ status, search, limit: String(limit) })
  return http.get(`/api/v1/admin/tasks?${query}`)
}

export function getOverview(): Promise<AdminOverview> { return http.get(`/api/v1/admin/overview?tz_offset_minutes=${new Date().getTimezoneOffset()}`) }
export function getPerformance(): Promise<AdminPerformance> { return http.get('/api/v1/admin/performance') }
export function getResources(): Promise<AdminResources> { return http.get('/api/v1/admin/resources') }
export function getEvents(level = 'all', module = 'all', search = '', sinceHours = 24): Promise<AdminEvent[]> {
  const query = new URLSearchParams({ level, module, search, since_hours: String(sinceHours) })
  return http.get(`/api/v1/admin/events?${query}`)
}

export function getTaskMetrics(): Promise<TaskMetrics> {
  return http.get('/api/v1/admin/task-metrics')
}

export function getTaskActivity(): Promise<TaskActivity[]> {
  return http.get('/api/v1/admin/task-activity')
}

export function cancelTask(id: string): Promise<{ id: string; status: string }> {
  return http.post(`/api/v1/admin/tasks/${id}/cancel`)
}

export function retryTask(id: string): Promise<{ id: string; status: string; attempt_no: number }> {
  return http.post(`/api/v1/admin/tasks/${id}/retry`)
}

export function cleanupStaleTemp(): Promise<{ deleted_count: number; deleted_bytes: number; skipped_count: number; older_than_days: number }> {
  return http.post('/api/v1/admin/resources/cleanup-temp')
}

export function listWorkers(): Promise<WorkerStatus[]> {
  return http.get('/api/v1/admin/workers')
}

export function getQueueStatus(): Promise<QueueStatus> {
  return http.get('/api/v1/admin/queue')
}
