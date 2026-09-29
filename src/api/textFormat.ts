/** Chapter-review workbench endpoints: server-side flow orchestration,
 * aggregated state, review marks, and version-bound reads/exports.
 *
 * All state lives in the backend (TextFormatFlow + task records); this client
 * only submits user intents (start/continue/retry marks) and reads.
 */
import { API_BASE, http } from './client'

export interface WorkbenchMatter {
  id: string
  scope: 'chapter' | 'version'
  reason: string
  advisory: boolean
  text: string
  chapter_key?: string
  detail?: string
}

export interface WorkbenchChapter {
  key: string | null
  seq: number
  num: number | null
  numStr: string
  title: string
  chars: number
  orig_num: number | null
  orig_numStr: string
  final_num: number | null
  actions: string[]
  reasons: string[]
  confidence: string
  pending: boolean
  adjusted: boolean
  matters: string[]
}

export interface WorkbenchVersion {
  flow_id: string
  task_id: string
  mode: 'smart' | 'by_length' | 'whole_book' | string
  version_status: 'current' | 'stale'
  created_at: string | null
  total_chars: number
  chapters: WorkbenchChapter[]
  files: { file_id?: string; name: string; chars: number }[]
  matters: WorkbenchMatter[]
  report: {
    actions: Array<Record<string, unknown>>
    warnings: Array<{ type?: string; detail?: string; [k: string]: unknown }>
    removed: Array<{ seq?: number; num?: number; numStr?: string; title?: string; kind?: string; [k: string]: unknown }>
  }
  baseline_chars: number | null
  original_count: number | null
  length_target: number | null
  review_marks: string[]
}

export interface WorkbenchFlow {
  id: string
  source_file_id: string
  /** Display name of the source TXT (set once the backend resolves it). */
  source_file_name?: string | null
  config_snapshot: Record<string, unknown>
  whole_book: boolean
  format_task_id: string | null
  analyze_task_id: string | null
  split_task_id: string | null
  split_mode: string | null
  status: 'running' | 'ready' | 'failed'
  error: string | null
  manifest_count: number
  created_at: string | null
  updated_at: string | null
}

export interface WorkbenchNextTask {
  stage: 'format' | 'analyze' | 'split' | null
  task_id: string
  task_type: string
  status: string
  progress: number
  failed?: boolean
}

export interface WorkbenchState {
  flow: WorkbenchFlow | null
  version: WorkbenchVersion | null
  next_task: WorkbenchNextTask | null
  active_tasks: { id: string; task_type: string; status: string; progress: number }[]
}

interface ApiErrorBody {
  detail?: { message?: string; [k: string]: unknown } | string
}

/** Extract the human-readable message from a workbench error body. */
export function workbenchErrorMessage(status: number, body: unknown): string {
  const detail = (body as ApiErrorBody | null)?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') return String(detail.message ?? status)
  return `请求失败（${status}）`
}

export interface FlowRequest {
  source_file_id?: string | null
  config?: Record<string, unknown>
  whole_book?: boolean
  restart?: boolean
}

const base = (projectId: string) => `/api/v1/projects/${projectId}/text-format`

export function getWorkbenchState(projectId: string): Promise<WorkbenchState> {
  return http.get<WorkbenchState>(base(projectId) + '/state')
}

export function postWorkbenchFlow(projectId: string, body: FlowRequest): Promise<WorkbenchState> {
  return http.post<WorkbenchState>(base(projectId) + '/flow', body)
}

export function postReviewMark(projectId: string, taskId: string, chapterKey: string): Promise<{ chapter_key: string }> {
  return http.post(base(projectId) + '/review-marks', { task_id: taskId, chapter_key: chapterKey })
}

export function deleteReviewMark(projectId: string, taskId: string, chapterKey: string): Promise<{ chapter_key: string }> {
  return http.del(base(projectId) + `/review-marks?task_id=${encodeURIComponent(taskId)}&chapter_key=${encodeURIComponent(chapterKey)}`)
}

/** Versioned chapter-text read (flow_id binds the manifest generation). */
export function previewUrl(projectId: string, flowId: string, name: string, download = false): string {
  const params = new URLSearchParams({ flow_id: flowId })
  if (download) params.set('download', 'true')
  return `${API_BASE}${base(projectId)}/file/${encodeURIComponent(name)}?${params.toString()}`
}

/** Whole-version export: the complete zip, validated before streaming. */
export function zipUrl(projectId: string, flowId: string): string {
  return `${API_BASE}${base(projectId)}/zip?flow_id=${encodeURIComponent(flowId)}`
}
