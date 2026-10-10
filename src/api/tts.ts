import { listQuery, type ListQuery, type ListPagination } from '@/api/listPaging'
import { http } from './client'
import { submitTaskBatches } from './batchSubmission'
import type {
  TTSStatus,
  VoicesListResult,
  PrepareFoundationsOptions,
  GenerateVoiceCandidatesOptions,
  BatchRunOptions,
  BatchStatusFiles,
  MergeStatusPackages,
  MergeSpeakersResult,
  MergeGraph,
  MergeBatchResult,
} from '@/types'

export interface BatchTaskSubmission {
  task_ids: string[]
  task_id?: string
  batch_id?: string
}

/** Report whether TTS is implemented (ready vs. engine-not-installed). */
export function ttsStatus(): Promise<TTSStatus> {
  return http.get<TTSStatus>('/api/tts/status')
}

/** 角色配音 · 阶段 1（LLM only）：start the voice-foundation Task (all / new-only / a subset). */
export function prepareFoundations(opts: PrepareFoundationsOptions = {}): Promise<BatchTaskSubmission> {
  return submitTaskBatches<BatchTaskSubmission>('/api/tts/prepare-foundations', {
    speakers: opts.speakers ?? null,
    new_only: opts.new_only ?? false,
    overrides: opts.overrides ?? null,
    script: opts.script ?? null,
  }, 'speakers', undefined, async () => (await listVoices(opts.script, undefined, undefined, false, true)).speakers.map(row => row.name))
}

/** 角色配音 · 阶段 2（TTS only）：start the clone-seed Task (all / new-only / a subset; N parallel). */
export function generateVoiceCandidates(opts: GenerateVoiceCandidatesOptions = {}): Promise<BatchTaskSubmission> {
  return submitTaskBatches<BatchTaskSubmission>('/api/tts/make-clones', {
    speakers: opts.speakers ?? null,
    new_only: opts.new_only ?? false,
    concurrency: opts.concurrency ?? null,
    script: opts.script ?? null,
    candidate_count: opts.candidate_count ?? null,
  }, 'speakers', undefined, async () => (await listVoices(opts.script, undefined, undefined, false, true)).speakers.map(row => row.name))
}

/** 角色配音：detected characters + voice-config state + preview paths (for a given script). */
export function listVoices(script?: string, options?: ListQuery, signal?: AbortSignal, summaryOnly = false, keysOnly = false): Promise<VoicesListResult & { pagination?: ListPagination }> {
  return http.get(`/api/tts/voices?${listQuery(options, { script, summary_only: summaryOnly || undefined, keys_only: keysOnly || undefined })}`, { signal })
}
export function batchList(options: ListQuery, signal?: AbortSignal, keysOnly = false, selected: string[] = []): Promise<BatchStatusFiles & { pagination?: ListPagination; missing_selected?: string[] }> {
  const url = `/api/tts/batch-list?${listQuery(options, { keys_only: keysOnly || undefined })}`
  return selected.length ? http.post(url, selected, { signal }) : http.get(url, { signal })
}
export function mergeList(options: ListQuery, signal?: AbortSignal, keysOnly = false, selected: string[] = []): Promise<MergeStatusPackages & { pagination?: ListPagination; missing_selected?: string[] }> {
  const url = `/api/tts/merge-list?${listQuery(options, { keys_only: keysOnly || undefined })}`
  return selected.length ? http.post(url, selected, { signal }) : http.get(url, { signal })
}

/** 角色配音：记录用户对某角色克隆候选的选择（单选一个为最终音色；audioId 为 null =
 *  清除选择，回默认第一条）。同步写（非任务）：选中的候选成为生效的克隆参考。 */
export function selectVoice(speaker: string, audioId: string | null): Promise<{
  ok: boolean
  speaker: string
  selected_audio_id: string | null
  ref_audio: string
}> {
  return http.put<{ ok: boolean; speaker: string; selected_audio_id: string | null; ref_audio: string }>(
    '/api/tts/voices/select', { speaker, audio_id: audioId },
  )
}

/** 角色配音：合并角色 —— 把 source 的台词在 Parse 源数据里全部改为 target（直接改写
 *  03_parsed_json/*.json，零 LLM 调用），删除 source 的声音配置（候选音频文件留盘），
 *  指向 source 的别名改指 target。同步写（非任务）；script 语义同 listVoices
 *  （''/undefined → 最近基文件，'__all__' → 所有基文件）。 */
export function mergeSpeakers(source: string, target: string, script?: string): Promise<MergeSpeakersResult> {
  return http.post<MergeSpeakersResult>('/api/tts/voices/merge-speakers', {
    source,
    target,
    script: script || null,
  })
}

const mergeQuery = (script?: string) => (script ? `script=${encodeURIComponent(script)}` : '')

/** 批量合并疑似角色：指向关系表 + 合并记录 + 否决 + 未查看的新增候选（含乐观锁 version）。 */
export function getMergeGraph(script?: string, signal?: AbortSignal): Promise<MergeGraph> {
  return http.get(`/api/tts/voices/merge-graph?${mergeQuery(script)}`, { signal })
}
/** 原子批量合并：sources 全部并入 target（任何一个失败则全部不生效）；version 过期 → 409。 */
export function mergeBatch(script: string | undefined, target: string, sources: string[], version: number): Promise<MergeBatchResult> {
  return http.post('/api/tts/voices/merge-batch', { script: script || null, target, sources, version })
}
/** 撤销一条合并记录（台词已变化则 409）。 */
export function undoMerge(script: string | undefined, recordId: string, version: number): Promise<{ ok: boolean; source: string; target: string; graph: MergeGraph }> {
  return http.post('/api/tts/voices/merge-undo', { script: script || null, record_id: recordId, version })
}
/** 忽略：source 不是 target（此后不再互相匹配，source 按无主角色重新匹配）。 */
export function vetoLink(script: string | undefined, source: string, target: string, version: number): Promise<{ ok: boolean; graph: MergeGraph }> {
  return http.post('/api/tts/voices/link-veto', { script: script || null, source, target, version })
}
/** 恢复被忽略的提示。 */
export function restoreLink(script: string | undefined, source: string, target: string, version: number): Promise<{ ok: boolean; restored: boolean; graph: MergeGraph }> {
  const query = [mergeQuery(script), `source=${encodeURIComponent(source)}`, `target=${encodeURIComponent(target)}`, `version=${version}`].filter(Boolean).join('&')
  return http.del(`/api/tts/voices/link-veto?${query}`)
}
/** 标记某目标角色的新增候选已查看（清除「新增」「待复核」）。 */
export function markMergeReviewed(script: string | undefined, target: string): Promise<{ ok: boolean; cleared: boolean; version: number }> {
  return http.post('/api/tts/voices/merge-review-seen', { script: script || null, target })
}

/** 角色配音：记录用户对某角色性别的标记（人名旁的 ♂/♀ 徽章）。同步写（非任务）；
 *  gender = 'male' | 'female' | ''（'' = 清除标记，回到未定）。 */
export function setGender(speaker: string, gender: 'male' | 'female' | ''): Promise<{
  ok: boolean
  speaker: string
  gender: string
}> {
  return http.post<{ ok: boolean; speaker: string; gender: string }>(
    '/api/tts/voices/gender', { speaker, gender },
  )
}

/** 音频合成：start a batch TTS Task (all lines, or the given line indices; for a script —
 *  or a whole selection of scripts, the 待合成 card's multi-select: each file is an independent
 *  durable task with its own package). The run is a resume: it skips segments
 *  already done (omitted → the administrator-configured TTS defaults for concurrency / seed;
 *  ``seed`` -1 → random). Re-doing everything = call :func:`submitBatchReset` first (deletes the
 *  packages), then this exact same call. */
export function runBatch(opts: BatchRunOptions = {}, key?: string, projectId?: string): Promise<BatchTaskSubmission> {
  return http.post<BatchTaskSubmission>('/api/tts/batch', {
    indices: opts.indices ?? null,
    script: opts.script ?? null,
    scripts: opts.scripts ?? null,
    project_id: projectId,
  }, { headers: key ? { 'Idempotency-Key': key } : {} })
}

/** Queue package removal before the ordinary synthesis task recreates each segment. */

export function submitBatchReset(scripts: string[], key?: string, projectId?: string): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/batch-reset', { scripts, project_id: projectId }, { headers: key ? { 'Idempotency-Key': key } : {} })
}

export function getSubmission(key: string): Promise<BatchTaskSubmission & { project_id: string; task_type: string; statuses: Record<string, string> }> {
  return http.get(`/api/v1/tasks/submissions/${encodeURIComponent(key)}`)
}

/** 音频合成进度（每文件）：each file's 【已合成 / 总段落】· 角色 · 已就绪声音, plus the
 *  【已合成】 flag when a file's segments are all done. Reads the package manifests (written
 *  incrementally as synthesis proceeds), so polling while a run streams gives live, real
 *  per-row counts. Send file names in the body so large books do not exceed HTTP URL limits. */
export async function batchStatusFiles(scripts: string[]): Promise<BatchStatusFiles> {
  const files: BatchStatusFiles['files'] = []
  for (let offset = 0; offset < scripts.length; offset += 100) {
    const response = await http.post<BatchStatusFiles>('/api/tts/batch-status', { scripts: scripts.slice(offset, offset + 100) })
    files.push(...(response.files ?? []))
  }
  return { files }
}

/** 音频合并：start one merge Task per selected package (always MP3 — the M4B half-branch
 *  is retired, A6). ``packages`` are sub-folder names in 05_audio_chunk/; the backend
 *  runs them in parallel under a CPU-sized merge gate (one Task each, dispatched in order). */
export interface MergeSubmissionReceipt {
  batch_id: string
  task_ids: string[]
  packages: { package: string; task_id: string }[]
}
export function runMerge(packages: string[] = [], idempotencyKey?: string, projectId?: string): Promise<MergeSubmissionReceipt> {
  return http.post<MergeSubmissionReceipt>(
    '/api/tts/merge', { packages, ...(projectId ? { project_id: projectId } : {}) },
    { headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {} },
  )
}

/** 音频合并进度（每包）：each package's 【已合成 / 总段数】→ 已就绪 readiness. ``total``
 *  comes from the source parsed JSON (never the manifest length — see the backend), so a
 *  mid-cancelled synthesis is not reported ready. Package names are carried in the JSON
 *  request body to support large package lists. */
export function mergeStatusPackages(packages: string[]): Promise<MergeStatusPackages> {
  return http.post<MergeStatusPackages>('/api/tts/merge-status', { packages })
}
