import { API_BASE, http } from './client'
import type { GenerateFilesResult, ParseChecks } from '@/types'

/**
 * Start one independent parse Task per selected file. ``names`` are bare file names in
 * ``02_split_text/``; the backend reads them, runs the LLM → JSON pipeline per file
 * (concurrency-bounded by ``generation.max_concurrency``), and returns the created task
 * ids to stream via the task store. Each file gets its own status / success / output.
 *
 * The pipeline includes the in-parse LLM check stages (chunk 忠实性校验 / instruct 检查 /
 * 角色匹配检查 / 断句失败校验 / 超长段落检查 / 归属抽样). ``checks`` 是用户在解析页勾选
 * 的 6 项开关：随请求提交、固化进每个任务的配置快照——任务执行期间不再跟随实时配置，
 * 重跑（重新提交）才用新值；缺省 = 沿用当前生效配置。
 */
export function generateScriptFiles(names: string[], checks?: ParseChecks): Promise<GenerateFilesResult> {
  return http.post<GenerateFilesResult>('/api/script/generate-files', { files: names, checks })
}

/** 【取消全部】：cancel the given tasks AND stop their batch(es) from dispatching
 *  further files (the backend's per-task cancel cannot express "stop dispatch").
 *  Terminal / unknown ids are ignored (idempotent). */
export function cancelParseBatch(taskIds: string[]): Promise<{ cancelled: unknown[]; batches_stopped: number }> {
  return http.post<{ cancelled: unknown[]; batches_stopped: number }>('/api/script/cancel-batch', { task_ids: taskIds })
}

// ---------------------------------------------------------------------------
// v1 project-scoped parse workbench (章节维度页面)
// ---------------------------------------------------------------------------

/** 最近一次任务（全状态、无窗口/隐藏；执行状态与结果可用性分开建模）。 */
export interface ScriptParseTaskRef {
  id: string
  status: string
  progress: number
  error: string
  created_at: string | null
  finished_at: string | null
}

export interface ScriptParseInput {
  file_id?: string
  name: string
  /** 当前内容摘要；null = 未登记的历史磁盘文件（提交时才登记）。 */
  sha256: string | null
  size: number | null
}

export interface ScriptParseResultRef {
  task_id: string
  name?: string
  file_id?: string
  /** 产物当前摘要（经文件表核验）；核验失败为 null。 */
  sha256: string | null
  size: number | null
  /** 该次解析读取的输入摘要（旧任务可能缺失）。 */
  source_sha256: string | null
  finished_at: string | null
  verified: boolean
}

export interface ScriptParseFile {
  name: string
  input: ScriptParseInput | null
  latest_task: ScriptParseTaskRef | null
  result: ScriptParseResultRef | null
  /** usable | stale | unverified | null（无成功结果）。 */
  result_status: 'usable' | 'stale' | 'unverified' | null
}

export interface ScriptParseChapterLite {
  key: string | null
  seq: number
  numStr: string
  title: string
  chars: number
}

export interface ScriptParseState {
  source: {
    mode: 'version' | 'legacy' | 'empty'
    version?: {
      flow_id: string
      version_status: 'current' | 'stale'
      chapters: ScriptParseChapterLite[]
      files: { file_id?: string; name: string; chars: number }[]
    }
    legacy_files?: { name: string; file_id?: string; sha256?: string; size?: number }[]
    disk_only?: string[]
  }
  text_format_busy: boolean
  files: ScriptParseFile[]
}

/** 只读聚合：章节清单 + 每章最新任务/结果可用性。轮询安全（无 flow 副作用）。 */
export function getScriptParseState(projectId: string, signal?: AbortSignal): Promise<ScriptParseState> {
  return http.get<ScriptParseState>(`/api/v1/projects/${projectId}/script-parse/state`, { signal })
}

/** 版本化批量提交：files 携带页面所见的 sha256（历史无摘要文件可缺省）；
 *  输入变更 / 分册发布中 / 同文件任务在途 → 409（detail.changed 列出变更文件）。 */
export function runScriptParse(
  projectId: string,
  files: { name: string; sha256?: string | null }[],
  checks?: ParseChecks,
): Promise<{ task_ids: string[]; files: { name: string; task_id: string; input_sha256?: string | null }[] }> {
  return http.post(`/api/v1/projects/${projectId}/script-parse/run`, { files, checks })
}

/** 项目绑定的解析结果读取 URL（ETag = 产物 sha256；内容变化必然 ETag 变化）。 */
export function scriptParseResultUrl(projectId: string, fileId: string): string {
  return `${API_BASE}/api/v1/projects/${encodeURIComponent(projectId)}/script-parse/results/${encodeURIComponent(fileId)}`
}
