// Browser-only layout samples. No administrator session or real API is used.
const now = '2026-10-04T00:00:00Z'
const rows = Array.from({ length: 80 }, (_, i) => i)
const queue = { available: true, stream: 'fixture', length: 80, pending: 5 }
const api = { window_seconds: 60, request_count: 80, server_error_count: 0, error_rate: 0, average_ms: 12, p95_ms: 20, status_counts: {}, scope: '隔离布局样本', endpoints: [] }
const system = { cpu_percent: 12, ram_used_bytes: 1024, ram_total_bytes: 8192, disk_total_bytes: 8192, disk_used_bytes: 1024, disk_free_bytes: 7168, uptime_seconds: 120 }
const workers = { online_workers: 4, total_slots: 4, active_slots: 1, idle_slots: 3 }
const metrics = { total: 80, status_counts: { running: 1, failed: 5, succeeded: 74 }, stage_counts: { production: 80, queued: 0, consuming: 1, completed: 74, attention: 5 }, throughput_60s: { window_seconds: 60, submitted: 80, started: 80, completed: 74, failed: 5 }, worker_pool: workers, by_type: [], generated_at: now }
const events = rows.map(i => ({ id: `layout-event-${i}`, time: now, level: 'error', module: 'fixture', type: '布局样本', message: '隔离样本：用于验证加载后长错误说明不会撑开外层页面。'.repeat(12) }))

export function adminLayoutResponse(path: string): unknown | undefined {
  if (path.endsWith('/overview')) return { generated_at: now, services: rows.slice(0, 6).map(i => ({ key: String(i), name: `测试服务 ${i}`, status: 'healthy', detail: '隔离状态说明'.repeat(20) })), today: { completed: 74, failed: 5, active_users: 1, api_requests: 80, llm_tokens: null, tts_characters: null, api_requests_scope: '隔离数据' }, tasks: { running: 1, queued: 0, failed: 5 }, workers, queue, api, system, gpu: [], user_count: 80, recent_errors: events }
  if (path.endsWith('/performance')) return { generated_at: now, system, gpu: [], tasks: metrics, workers: rows.map(i => ({ worker_id: `layout-worker-${i}`, status: 'online', capabilities: { slots: 1, active_slots: 0, task_types: ['tts.merge'] }, current_task_id: null, last_seen_at: now })), queue, api, unavailable_metrics: [] }
  if (path.endsWith('/task-metrics')) return metrics
  if (path.endsWith('/users')) return rows.map(i => ({ id: `layout-user-${i}`, username: `layout_user_${i}`, email: `layout_${i}@example.test`, display_name: '长用户名称布局检查'.repeat(8), role: 'user', is_active: true, created_at: now, project_count: 1, storage_bytes: 1024, available_units: 0, reserved_units: 0, consumed_units: 0 }))
  if (path.endsWith('/tasks')) return rows.map(i => ({ id: `layout-task-${i}`, owner_username: 'layout_user', task_type: 'tts.merge', status: 'failed', progress: 0.2, error_message: events[i].message, created_at: now, updated_at: now, project_id: 'layout-project' }))
  if (path.endsWith('/events')) return events
  if (path.endsWith('/resources')) return { root_path: '隔离布局样本', disk_total_bytes: 8192, disk_used_bytes: 1024, disk_free_bytes: 7168, projects: 80, files: [{ kind: 'audio', count: 80, size_bytes: 1024 }], users: rows.map(i => ({ username: `layout_user_${i}`, project_count: 1, file_count: 1, size_bytes: 1024 })), scope: '隔离样本', music_library: { count: 80, size_bytes: 1024 }, project_storage: { categories: [], cleanup_candidates: { count: 0, size_bytes: 0, older_than_days: 7 } } }
  if (path.endsWith('/settings/storage')) return { root_path: '隔离样本', source: 'admin' }
  if (path.endsWith('/settings/quota')) return { initial_units: 0, source: 'admin' }
  if (path.endsWith('/settings/registration')) return { enabled: true, source: 'admin' }
  if (path.endsWith('/settings/runtime')) return { limits: { max_upload_bytes: 1024, session_ttl_hours: 24, task_lease_seconds: 60, task_max_attempts: 3 }, source: 'deployment-environment', editable_in_console: false, model_settings_scope: 'platform' }
  if (path.endsWith('/settings/application')) return { config: { paths: { working_dir: '' }, text: {}, split: { length_target: 8000, smart_split_long_chapters: true }, ffmpeg: { ffmpeg_path: '', ffprobe_path: '' }, audio: { target_duration: '20:00', naming_format: '{}.mp3', start_number: '1', smart_align: true, align_tolerance: 10 }, tts: { batch_concurrency: 4, batch_auto: false, batch_seed: -1 }, llm: { base_url: '', api_key: '', model_name: '隔离样本' }, prompts: { system_prompt: '隔离提示词布局样本。'.repeat(300), user_prompt: '隔离样本' }, persona_prompts: { system_prompt: '', user_prompt: '' }, generation: { banned_tokens: [], chunk_size: 2000, max_tokens: 2000 }, bgm: { mode: 'random', volume: 0.15, fade_in: 2, fade_out: 2 }, ui: { theme: 'light', show_audio_split: true } } }
  if (path.endsWith('/settings/gpu-scheduler')) return { config: { enabled: false, scheduler_interval: 1, min_service_runtime: 1, switch_cooldown: 1, queue_difference_threshold: 1, max_wait_time: 1, shutdown_when_idle: true, idle_shutdown_timeout: 1, startup_timeout: 1, service_stop_timeout: 1, drain_timeout: 1, health_check_interval: 1, gpu_release_wait: 1, startup_retry_count: 1, llm_start_script_path: '', llm_stop_script_path: '' } }
  if (path.endsWith('/gpu-scheduler/status')) return { enabled: false, state: 'disabled', phase: '', current: null, llm: { waiting: 0, running: 0, oldest_wait: 0, pressure: 0 }, tts: { waiting: 0, running: 0, oldest_wait: 0, pressure: 0 }, runtime: 0, last_switch: null, reason: '隔离布局样本', error: '', heartbeat_age: 0, stale: false }
  return undefined
}
