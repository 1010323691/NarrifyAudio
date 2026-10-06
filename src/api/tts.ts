import { http } from './client'
import type {
  TTSStatus,
  VoicesListResult,
  PrepareFoundationsOptions,
  GenerateVoiceCandidatesOptions,
  BatchRunOptions,
  BatchStatusFiles,
  MergeStatusPackages,
  MergeSpeakersResult,
} from '@/types'

export interface BatchTaskSubmission {
  task_ids: string[]
  task_id?: string
}

/** Report whether TTS is implemented (ready vs. engine-not-installed). */
export function ttsStatus(): Promise<TTSStatus> {
  return http.get<TTSStatus>('/api/tts/status')
}

/** 角色配音 · 阶段 1（LLM only）：start the voice-foundation Task (all / new-only / a subset). */
export function prepareFoundations(opts: PrepareFoundationsOptions = {}): Promise<BatchTaskSubmission> {
  return http.post<BatchTaskSubmission>('/api/tts/prepare-foundations', {
    speakers: opts.speakers ?? null,
    new_only: opts.new_only ?? false,
    overrides: opts.overrides ?? null,
    script: opts.script ?? null,
  })
}

/** 角色配音 · 阶段 2（TTS only）：start the clone-seed Task (all / new-only / a subset; N parallel). */
export function generateVoiceCandidates(opts: GenerateVoiceCandidatesOptions = {}): Promise<BatchTaskSubmission> {
  return http.post<BatchTaskSubmission>('/api/tts/make-clones', {
    speakers: opts.speakers ?? null,
    new_only: opts.new_only ?? false,
    concurrency: opts.concurrency ?? null,
    script: opts.script ?? null,
    candidate_count: opts.candidate_count ?? null,
  })
}

/** 角色配音：detected characters + voice-config state + preview paths (for a given script). */
export function listVoices(script?: string): Promise<VoicesListResult> {
  const q = script ? `?script=${encodeURIComponent(script)}` : ''
  return http.get<VoicesListResult>(`/api/tts/voices${q}`)
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
export function runBatch(opts: BatchRunOptions = {}): Promise<BatchTaskSubmission> {
  return http.post<BatchTaskSubmission>('/api/tts/batch', {
    indices: opts.indices ?? null,
    script: opts.script ?? null,
    scripts: opts.scripts ?? null,
  })
}

/** Queue package removal before the ordinary synthesis task recreates each segment. */

export function submitBatchReset(scripts: string[]): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/batch-reset', { scripts })
}

/** 音频合成进度（每文件）：each file's 【已合成 / 总段落】· 角色 · 已就绪声音, plus the
 *  【已合成】 flag when a file's segments are all done. Reads the package manifests (written
 *  incrementally as synthesis proceeds), so polling while a run streams gives live, real
 *  per-row counts. Send file names in the body so large books do not exceed HTTP URL limits. */
export function batchStatusFiles(scripts: string[]): Promise<BatchStatusFiles> {
  return http.post<BatchStatusFiles>('/api/tts/batch-status', { scripts })
}

/** 音频合并：start one merge Task per selected package (always MP3 — the M4B half-branch
 *  is retired, A6). ``packages`` are sub-folder names in 05_audio_chunk/; the backend
 *  runs them in parallel under a CPU-sized merge gate (one Task each, dispatched in order). */
export function runMerge(packages: string[] = []): Promise<{
  task_ids: string[]
  packages: { package: string; task_id: string }[]
}> {
  return http.post<{ task_ids: string[]; packages: { package: string; task_id: string }[] }>(
    '/api/tts/merge', { packages },
  )
}

/** 音频合并进度（每包）：each package's 【已合成 / 总段数】→ 已就绪 readiness. ``total``
 *  comes from the source parsed JSON (never the manifest length — see the backend), so a
 *  mid-cancelled synthesis is not reported ready. Package names are carried in the JSON
 *  request body to support large package lists. */
export function mergeStatusPackages(packages: string[]): Promise<MergeStatusPackages> {
  return http.post<MergeStatusPackages>('/api/tts/merge-status', { packages })
}
