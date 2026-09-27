import { http } from './client'
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
