import { http } from './client'
import type {
  TTSStatus,
  VoicesListResult,
  PrepareFoundationsOptions,
  MakeClonesOptions,
  BatchRunOptions,
  BatchStatusFiles,
  MergeStatusPackages,
  MergeSpeakersResult,
} from '@/types'

/** Report whether TTS is implemented (ready vs. engine-not-installed). */
export function ttsStatus(): Promise<TTSStatus> {
  return http.get<TTSStatus>('/api/tts/status')
}

/** 角色配音 · 阶段 1（LLM only）：start the voice-foundation Task (all / new-only / a subset). */
export function prepareFoundations(opts: PrepareFoundationsOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/prepare-foundations', {
    speakers: opts.speakers ?? null,
    new_only: opts.new_only ?? false,
    overrides: opts.overrides ?? null,
    script: opts.script ?? null,
  })
}

/** 角色配音 · 阶段 2（TTS only）：start the clone-seed Task (all / new-only / a subset; N parallel). */
export function makeClones(opts: MakeClonesOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/make-clones', {
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
 *  or a whole selection of scripts, the 待合成 card's multi-select: one task synthesizes
 *  the files one by one, each in its own package). The run is a resume: it skips segments
 *  already done (omitted → the administrator-configured TTS defaults for concurrency / seed;
 *  ``seed`` -1 → random). Re-doing everything = call :func:`resetBatch` first (deletes the
 *  packages), then this exact same call. */
export function runBatch(opts: BatchRunOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/tts/batch', {
    indices: opts.indices ?? null,
    script: opts.script ?? null,
    scripts: opts.scripts ?? null,
  })
}

/** 音频合成 · 重新全部合成 · 第一步（同步、非任务）：删除选中文件的合成包
 *  （``05_audio_chunk/<包>/``——逐行 mp3 + manifest），随后的「一键音频合成」（默认续合
 *  语义，与一键合成完全同一条线路）即从头重做全部段落。 */
export function resetBatch(scripts: string[]): Promise<{ ok: boolean; removed: string[] } | { task_id: string }> {
  return http.post<{ ok: boolean; removed: string[] } | { task_id: string }>('/api/tts/batch-reset', { scripts })
}

/** 音频合成进度（每文件）：each file's 【已合成 / 总段落】· 角色 · 已就绪声音, plus the
 *  【已合成】 flag when a file's segments are all done. Reads the package manifests (written
 *  incrementally as synthesis proceeds), so polling while a run streams gives live, real
 *  per-row counts. FastAPI's ``list[str]`` query param = one repeated ``scripts=`` per file. */
export function batchStatusFiles(scripts: string[]): Promise<BatchStatusFiles> {
  const q = new URLSearchParams()
  for (const s of scripts) q.append('scripts', s)
  return http.get<BatchStatusFiles>(`/api/tts/batch-status?${q.toString()}`)
}

/** 音频合并：start one merge Task per selected package (MP3 now; M4B is a later phase).
 *  ``packages`` are sub-folder names in 05_audio_chunk/; the backend runs them in
 *  parallel under a CPU-sized merge gate (one Task each, dispatched in order). */
export function runMerge(m4b = false, packages: string[] = []): Promise<{
  task_ids: string[]
  packages: { package: string; task_id: string }[]
}> {
  return http.post<{ task_ids: string[]; packages: { package: string; task_id: string }[] }>(
    '/api/tts/merge', { m4b, packages },
  )
}

/** 音频合并进度（每包）：each package's 【已合成 / 总段数】→ 已就绪 readiness. ``total``
 *  comes from the source parsed JSON (never the manifest length — see the backend), so a
 *  mid-cancelled synthesis is not reported ready. FastAPI's ``list[str]`` query param =
 *  one repeated ``packages=`` per package. */
export function mergeStatusPackages(packages: string[]): Promise<MergeStatusPackages> {
  const q = new URLSearchParams()
  for (const p of packages) q.append('packages', p)
  return http.get<MergeStatusPackages>(`/api/tts/merge-status?${q.toString()}`)
}
