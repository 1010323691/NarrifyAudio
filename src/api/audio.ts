import { http } from './client'
import type {
  AudioPlanResult,
  AudioProbeResult,
  AudioSegment,
} from '@/types'

/** Quick ffprobe: duration / size / extension / mime (synchronous, fast). */
export function probeAudio(path: string): Promise<AudioProbeResult> {
  return http.post<AudioProbeResult>('/api/audio/probe', { path })
}

/** Even-distribution plan (synchronous, pure math after a probe). */
export function planAudio(path: string, targetDuration?: string): Promise<AudioPlanResult> {
  return http.post<AudioPlanResult>('/api/audio/plan', {
    path,
    target_duration: targetDuration ?? null,
  })
}

/** Long-running: pause detection + pause-aligned plan. Returns a task id; the
 *  aligned result arrives via the task's SSE stream (see the store). */
export function detectSilences(
  path: string,
  opts: { targetDuration?: string; alignTolerance?: number } = {},
): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/audio/silences', {
    path,
    target_duration: opts.targetDuration ?? null,
    align_tolerance: opts.alignTolerance ?? null,
  })
}

export interface CutOptions {
  targetDuration?: string
  smartAlign?: boolean
  alignTolerance?: number
  namingFormat?: string
  startNumber?: string
  segments?: AudioSegment[]
}

/** Long-running: lossless ``-c copy`` cut to the workspace's 07_output/. Returns a task id. */
export function cutAudio(path: string, opts: CutOptions = {}): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/audio/cut', {
    path,
    target_duration: opts.targetDuration ?? null,
    smart_align: opts.smartAlign ?? null,
    align_tolerance: opts.alignTolerance ?? null,
    naming_format: opts.namingFormat ?? null,
    start_number: opts.startNumber ?? null,
    segments: opts.segments ?? null,
  })
}

/** One cut file, as the packaging/export endpoints expect it. */
export interface CutFileSpec {
  name: string
  path: string
}

export interface AudioZipResult {
  zip_path: string
  file_count: number
}

/** Build a zip of the given cut files into the workspace's 07_output/; the result's
 *  ``zip_path`` is served by ``/api/files/download/07_output/<name>`` for download. */
export function zipAudio(opts: { base?: string; files: CutFileSpec[] }): Promise<AudioZipResult> {
  return http.post<AudioZipResult>('/api/audio/zip', {
    base: opts.base ?? null,
    files: opts.files,
  })
}

export interface AudioExportResult {
  dest_dir: string
  file_count: number
  files: CutFileSpec[]
}

/** Copy the given cut files into a "分集" folder beside the source audio. */
export function exportAudio(
  sourcePath: string,
  opts: { files: CutFileSpec[] },
): Promise<AudioExportResult> {
  return http.post<AudioExportResult>('/api/audio/export', {
    source_path: sourcePath,
    files: opts.files,
  })
}
