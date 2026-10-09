import { listQuery, type ListPagination } from '@/api/listPaging'
import { API_BASE, http } from './client'

export interface GpuSchedulerConfig {
  enabled: boolean
  scheduler_interval: number
  min_service_runtime: number
  switch_cooldown: number
  queue_difference_threshold: number
  max_wait_time: number
  shutdown_when_idle: boolean
  idle_shutdown_timeout: number
  startup_timeout: number
  service_stop_timeout: number
  drain_timeout: number
  health_check_interval: number
  gpu_release_wait: number
  startup_retry_count: number
  llm_start_script_path: string
  llm_stop_script_path: string
}

export interface GpuSchedulerStatus {
  enabled: boolean
  state: string
  phase: string
  current: 'LLM' | 'TTS' | null
  llm: { waiting: number; running: number; oldest_wait: number; pressure: number }
  tts: { waiting: number; running: number; oldest_wait: number; pressure: number }
  runtime: number
  last_switch: number | null
  reason: string
  error: string
  heartbeat_age: number | null
  stale: boolean
}

export function getGpuSchedulerConfig(): Promise<{ config: GpuSchedulerConfig }> {
  return http.get('/api/v1/admin/settings/gpu-scheduler')
}

export function updateGpuSchedulerConfig(config: Partial<GpuSchedulerConfig>): Promise<{ config: GpuSchedulerConfig }> {
  return http.patch('/api/v1/admin/settings/gpu-scheduler', config)
}

export function getGpuSchedulerStatus(): Promise<GpuSchedulerStatus> {
  return http.get('/api/v1/admin/gpu-scheduler/status')
}

export function recoverGpuScheduler(): Promise<{ accepted: boolean }> {
  return http.post('/api/v1/admin/gpu-scheduler/recover')
}

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
  project_file_count?: number
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
export interface DbConnections { active: number; idle: number; idle_in_transaction: number; total: number; max: number }
export interface DbPool { size: number; checked_out: number; overflow: number; max_overflow: number }
export interface AdminOverview {
  generated_at: string; services: ServiceStatus[]
  today: { completed: number; failed: number; submitted: number; active_users: number; api_requests: number; tts_chars: number; llm_chars: number; api_requests_scope: string }
  tasks: { running: number; queued: number; failed: number }; workers: TaskMetrics['worker_pool']; queue: QueueStatus; api: ApiSnapshot
  system: { cpu_percent: number | null; ram_used_bytes: number | null; ram_total_bytes: number | null }
  database: DbConnections | null
  gpu: GpuStatus[]; user_count: number; recent_errors: AdminEvent[]
}
export interface HostMetrics {
  cpu_percent: number | null; ram_used_bytes: number | null; ram_total_bytes: number | null
  disk_used_bytes: number | null; disk_total_bytes: number | null; disk_free_bytes: number | null
  uptime_seconds: number | null; load_average_1m: number | null
}
export interface AdminPerformance {
  generated_at: string
  system: HostMetrics
  gpu: GpuStatus[]; tasks: TaskMetrics; queue: QueueStatus; api: ApiSnapshot
  api_pool: DbPool | null
  database: { counters: Record<string, number>; size_bytes: number | null; connections: DbConnections } | null
  /** LLM gate concurrency (configured). */
  llm_limit: number
  /** Whole LLM tasks admitted at once (gate x multiplier, covers mechanical stages). */
  llm_task_limit: number
  /** TTS batch pools in member tasks: one executing pool can hold hundreds of members. */
  tts_batch: { pools: number; active_members: number; parked_pools: number; parked_members: number; queued_members: number }
}
export interface ApiSnapshot {
  tts_stages?: { stage: string; count: number; p95_ms: number }[]
  window_seconds: number; request_count: number; server_error_count: number; error_rate: number
  average_ms: number | null; p95_ms: number | null; status_counts: Record<string, number>; scope: string
  endpoints: { route: string; requests: number; server_errors: number; error_rate: number; average_ms: number; p95_ms: number }[]
}
export interface AdminResources {
  light?: boolean
  pagination?: ListPagination
  root_path: string; disk_total_bytes: number; disk_used_bytes: number; disk_free_bytes: number
  projects: number; files: { kind: string; count: number; size_bytes: number }[]
  users: { username: string; project_count?: number; file_count?: number; count?: number; size_bytes: number; registered_file_count?: number; registered_file_bytes?: number }[]
  project_storage?: {
    size_bytes?: number; file_count?: number
    categories?: { kind: string; label: string; count: number; size_bytes: number }[]
    cleanup_candidates?: { count: number; size_bytes: number; older_than_days: number }
  }
  scope: string
  music_library: { count: number; size_bytes: number | null; assigned_chapters?: number }
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
  group?: string
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
  worker_pool: { online_workers: number; total_slots: number; active_slots: number; idle_slots: number; llm_gate_active?: number }
  by_type: TaskTypeMetric[]
  generated_at: string
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
  used_memory_bytes?: number
  connected_clients?: number
  ops_per_sec?: number
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

export function listLlmModels(params?: { base_url?: string; api_key?: string }): Promise<{ models: string[] }> {
  const query = new URLSearchParams()
  if (params?.base_url) query.set('base_url', params.base_url)
  if (params?.api_key) query.set('api_key', params.api_key)
  const qs = query.toString()
  return http.get(`/api/v1/admin/llm/models${qs ? `?${qs}` : ''}`)
}

export function updateRegistrationSettings(enabled: boolean): Promise<RegistrationSettings> {
  return http.patch('/api/v1/admin/settings/registration', { enabled })
}

export function adjustQuota(userId: string, amount: number, idempotency_key: string, note = ''): Promise<Record<string, number | string>> {
  return http.post(`/api/v1/admin/users/${userId}/quota/adjust`, { amount, idempotency_key, note })
}

export function getOverview(): Promise<AdminOverview> { return http.get(`/api/v1/admin/overview?tz_offset_minutes=${new Date().getTimezoneOffset()}`) }
export function getPerformance(): Promise<AdminPerformance> { return http.get('/api/v1/admin/performance') }
/** One page of worker heartbeats (counts.online = workers seen within the heartbeat timeout). */
export function workerPage(page: number, page_size: number, signal?: AbortSignal): Promise<{ items: WorkerStatus[]; pagination: ListPagination }> {
  return http.get(`/api/v1/admin/workers?${listQuery(undefined, { page, page_size })}`, { signal })
}
export function getResources(light = true, page = 1, page_size = 10, usersOnly = false): Promise<AdminResources> {
  return http.get(`/api/v1/admin/resources?${listQuery(undefined, { light, page, page_size, users_only: usersOnly || undefined })}`)
}
export function getTaskMetrics(): Promise<TaskMetrics> {
  return http.get('/api/v1/admin/task-metrics')
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

export function userPage(options: { page: number; page_size: number; search: string; role: string; state: string; sort: string }, signal?: AbortSignal): Promise<{ items: AdminUser[]; pagination: ListPagination }> {
  return http.get(`/api/v1/admin/users?${listQuery(undefined, options)}`, { signal })
}
export interface TaskFilters { status: string; search: string; group: string; task_type: string }
export function taskPage(filters: TaskFilters, page: number, page_size: number, signal?: AbortSignal): Promise<{ items: AdminTask[]; pagination: ListPagination }> {
  return http.get(`/api/v1/admin/tasks?${listQuery(undefined, { ...filters, page, page_size })}`, { signal })
}
export function eventPage(level: string, module: string, search: string, since_hours: number, page: number, page_size: number, signal?: AbortSignal): Promise<{ items: AdminEvent[]; pagination: ListPagination }> {
  return http.get(`/api/v1/admin/events?${listQuery(undefined, { level, module, search, since_hours, page, page_size })}`, { signal })
}

const tz = () => new Date().getTimezoneOffset()

export type HistoryRange = '1h' | '6h' | '24h' | '7d'
export type ThroughputRange = '24h' | '7d' | '30d'
/** Flat numeric keys per point (llm_running, db_inserted_ps, cpu_percent, gpu0_util …); absent = not sampled. */
export type MetricPoint = { time: string } & Record<string, number | string | undefined>
export interface MetricsHistory {
  range: HistoryRange; step_seconds: number; points: MetricPoint[]; sample_count: number
  latest_at: string | null; latest: Record<string, any> | null; scope: string
}
export interface ThroughputPoint {
  time: string; submitted: number; succeeded: number; failed: number; tts_chars: number; llm_chars: number
  [groupSubmitted: string]: number | string
}
export interface ThroughputTotals { submitted: number; succeeded: number; failed: number; tts_chars: number; llm_chars: number }
export interface TaskTypeStats {
  task_type: string; group: string; total: number; succeeded: number; failed: number; success_rate: number | null
  duration_avg_s: number | null; duration_p50_s: number | null; duration_p95_s: number | null; wait_p50_s: number | null; wait_p95_s: number | null
}
export interface Throughput {
  range: ThroughputRange; step_seconds: number; points: ThroughputPoint[]; totals: ThroughputTotals; previous_totals: ThroughputTotals
  by_type: TaskTypeStats[]; top_errors: { code: string; count: number; sample: string }[]; heatmap: number[][]; generated_at: string
}
export interface ApiSeries {
  minutes: number; step_minutes: number; scope: string
  points: { time: string; requests: number; business_requests: number; errors: number; rps: number; average_ms: number | null; p95_ms: number | null }[]
}
export interface EventStats { step_seconds: number; points: { time: string; error: number; info: number }[]; modules: { module: string; count: number }[]; total: number }
export interface AuditDetail { id: string; action: string; target_type: string; target_id: string; metadata: Record<string, unknown>; created_at: string; actor: { id: string; username: string } | null }
export interface TaskDetail extends AdminTask {
  owner_id: string; project_name: string | null; next_attempt_at: string | null; batch_id: string | null; group: string
  attempts: { attempt_no: number; worker_id: string; status: string; started_at: string | null; finished_at: string | null; error_message: string }[]
  events: { sequence: number; type: string; time: string; summary: string }[]
}
export interface UserDetail {
  id: string
  quota: { available_units: number; reserved_units: number; frozen_units: number; consumed_units: number } | null
  sessions: { active: number; last_seen_at: string | null }
  task_statuses: Record<string, number>
  recent_tasks: { id: string; task_type: string; status: string; progress: number; created_at: string; finished_at: string | null }[]
  daily_usage: { time: string; tts_chars: number; llm_chars: number; tasks: number }[]
}
export interface QuotaTransactionRow {
  id: string; time: string; kind: string; amount: number; resource_type: string | null; operation_type: string | null
  char_count: number | null; available_after: number | null; consumed_after: number | null; task_id: string | null; note: string
}
export interface NewUser { email: string; username: string; password: string; display_name: string; role: 'user' | 'admin' }
export interface BulkResult { action: 'cancel' | 'retry'; succeeded: number; results: { id: string; ok: boolean; changed?: boolean; status?: string; reason?: string }[] }

export function getMetricsHistory(range: HistoryRange, signal?: AbortSignal): Promise<MetricsHistory> {
  return http.get(`/api/v1/admin/metrics/history?range=${range}`, { signal })
}
export function getThroughput(range: ThroughputRange, signal?: AbortSignal): Promise<Throughput> {
  return http.get(`/api/v1/admin/analytics/throughput?range=${range}&tz_offset_minutes=${tz()}`, { signal })
}
export function getApiSeries(minutes: number, signal?: AbortSignal): Promise<ApiSeries> {
  return http.get(`/api/v1/admin/analytics/api?minutes=${minutes}`, { signal })
}
export function getEventStats(level: string, module: string, search: string, since_hours: number, signal?: AbortSignal): Promise<EventStats> {
  return http.get(`/api/v1/admin/events/stats?${listQuery(undefined, { level, module, search, since_hours, tz_offset_minutes: tz() })}`, { signal })
}
export function eventExportUrl(level: string, module: string, search: string, since_hours: number): string {
  return `${API_BASE}/api/v1/admin/events/export?${listQuery(undefined, { level, module, search, since_hours })}`
}
export function getAuditDetail(id: string): Promise<AuditDetail> { return http.get(`/api/v1/admin/events/audit/${id}`) }
export function getTaskDetail(id: string): Promise<TaskDetail> { return http.get(`/api/v1/admin/tasks/${id}`) }
export function bulkTasks(action: 'cancel' | 'retry', ids: string[]): Promise<BulkResult> {
  return http.post('/api/v1/admin/tasks/bulk', { action, ids })
}
export function createUser(payload: NewUser): Promise<AdminUser> { return http.post('/api/v1/admin/users', payload) }
export function getUserDetail(id: string): Promise<UserDetail> { return http.get(`/api/v1/admin/users/${id}?tz_offset_minutes=${tz()}`) }
export function userQuotaTransactions(id: string, page: number, page_size = 10): Promise<{ items: QuotaTransactionRow[]; pagination: ListPagination }> {
  return http.get(`/api/v1/admin/users/${id}/quota-transactions?page=${page}&page_size=${page_size}`)
}
export function revokeUserSessions(id: string): Promise<{ id: string; revoked: number }> {
  return http.post(`/api/v1/admin/users/${id}/sessions/revoke`)
}
