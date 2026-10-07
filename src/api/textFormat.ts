/** Chapter-review workbench endpoints: server-side flow orchestration,
 * aggregated state, review marks, and version-bound reads/exports.
 *
 * All state lives in the backend (TextFormatFlow + task records); this client
 * only submits user intents (start/continue/retry marks) and reads.
 */
import type { ListPagination, ListQuery } from '@/api/listPaging'
import { listQuery } from '@/api/listPaging'
import { API_BASE, http } from './client'

export interface WorkbenchMatter {
  id: string
  /** 版本级 matters 已随页顶 banner 一并移除（章节级提示足够），后端现只产出 chapter 级。 */
  scope: 'chapter'
  reason: string
  advisory: boolean
  text: string
  chapter_key?: string
  detail?: string
}

export interface WorkbenchChapter {
  file?: { file_id?: string; name: string; chars: number } | null
  dup_info?: { count: number; index: number } | null
  key: string | null
  seq: number
  num: number | null
  numStr: string
  title: string
  chars: number
  orig_num: number | null
  orig_numStr: string
  source_chapter_id?: string
  long_split?: {
    source_chars: number
    target_chars: number
    segment_index: number
    segment_count: number
    wanted_count: number
  }
  final_num: number | null
  actions: string[]
  reasons: string[]
  confidence: string
  pending: boolean
  adjusted: boolean
  matters: string[]
}

/** 分册方式两选一（处理设置弹窗）：智能分册 / 按字数分册。
 *  后端保留 whole_book 通道（历史数据兼容），UI 不提供该选项。 */
export type SplitMode = 'smart' | 'by_length'

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
  split_policy?: {
    enabled: boolean
    target_chars?: number
    threshold_chars?: number
    target_source?: 'normal_average' | 'length_target'
    normal_sample_count?: number
    length_target?: number
  } | null
  review_marks: string[]
}

export interface WorkbenchSourceFile {
  file_id: string
  name: string
  size: number
  available: boolean
}

export interface WorkbenchFlow {
  id: string
  source_file_id: string
  source_file_ids?: string[]
  source_files?: WorkbenchSourceFile[]
  /** Display name of the first source document. */
  source_file_name?: string | null
  config_snapshot: Record<string, unknown>
  whole_book: boolean
  /** 强制按字数分册（处理设置弹窗两选一选了「按字数分册」；历史 flow 的 whole_book 优先于它）。 */
  force_by_length: boolean
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
  pagination?: ListPagination
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
  source_file_ids?: string[]
  config?: Record<string, unknown>
  whole_book?: boolean
  force_by_length?: boolean
  restart?: boolean
}

const base = (projectId: string) => `/api/v1/projects/${projectId}/text-format`

export function getWorkbenchState(projectId: string, options?: { recover?: boolean; list?: ListQuery; reason?: string; origNum?: number | null; signal?: AbortSignal }): Promise<WorkbenchState> {
  const query = options?.list ? listQuery(options.list, { reason: options.reason ?? '', ...(options.origNum != null ? { orig_num: String(options.origNum) } : {}) }) : ''
  const params = [query, options?.recover === false ? 'recover=false' : ''].filter(Boolean).join('&')
  return http.get<WorkbenchState>(base(projectId) + '/state' + (params ? `?${params}` : ''), { signal: options?.signal })
}

export function postWorkbenchFlow(projectId: string, body: FlowRequest, list?: ListQuery): Promise<WorkbenchState> {
  return http.post<WorkbenchState>(base(projectId) + '/flow' + (list ? `?${listQuery(list)}` : ''), body)
}

export function postReviewMark(projectId: string, taskId: string, chapterKey: string): Promise<{ chapter_key: string }> {
  return http.post(base(projectId) + '/review-marks', { task_id: taskId, chapter_key: chapterKey })
}

export function deleteReviewMark(projectId: string, taskId: string, chapterKey: string): Promise<{ chapter_key: string }> {
  return http.del(base(projectId) + `/review-marks?task_id=${encodeURIComponent(taskId)}&chapter_key=${encodeURIComponent(chapterKey)}`)
}

/** Versioned chapter-text read (flow_id binds the manifest generation). */
export function previewUrl(projectId: string, flowId: string, name: string): string {
  const params = new URLSearchParams({ flow_id: flowId })
  return `${API_BASE}${base(projectId)}/file/${encodeURIComponent(name)}?${params.toString()}`
}
