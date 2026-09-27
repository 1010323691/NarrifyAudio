// BGM API (背景音乐：段落分析 / 匹配 / 手动干预 / 混音；后端 /api/bgm)。
// 瘦客户端：只做 HTTP 封装，无处理逻辑。

import { API_BASE, http } from './client'
import type {
  BgmChaptersResult,
  BgmMatchResult,
  BgmBatchResult,
  BgmAssignment,
  BgmPackageResult,
  BgmTimelineResult,
} from '@/types'

/** All chapter rows (disk-state basis = 02_split_text stems). Read-only. */
export function getChapters(): Promise<BgmChaptersResult> {
  return http.get<BgmChaptersResult>('/api/bgm/chapters')
}

/** Preview URL of a finished mix (08_bgm is a workspace dir — the shared
 *  files API serves it for the inline player). */
export function bgmPreviewUrl(stem: string): string {
  return `${API_BASE}/api/files/download/08_bgm/${encodeURIComponent(stem + '.mp3')}`
}

/** (Re-)match the selected chapters (empty/omitted = all) through the durable Worker. */
export function matchChapters(chapters: string[] | null, mode: string): Promise<BgmMatchResult | { task_id: string }> {
  return http.post<BgmMatchResult | { task_id: string }>('/api/bgm/match', { chapters, mode })
}

/** Start one paragraph-analysis Task per selected chapter (段落分析). */
export function analyzeSegmentChapters(chapters: string[]): Promise<BgmBatchResult> {
  return http.post<BgmBatchResult>('/api/bgm/analyze-segment', { chapters })
}

/** The chapter's full timeline file (the 时间轴 dialog's data source; 404 when
 *  the chapter has no timeline yet — the caller catches). */
export function getTimeline(stem: string): Promise<BgmTimelineResult> {
  return http.get<BgmTimelineResult>(`/api/bgm/timeline/${encodeURIComponent(stem)}`)
}

/** Manual edit of one chapter: music (null = clear) and/or lock.
 *  Omitted keys are untouched. */
export function updateChapter(
  stem: string,
  patch: { music?: string | null; locked?: boolean; __setMusic?: boolean },
): Promise<BgmAssignment> {
  const { __setMusic, ...rest } = patch
  const body: Record<string, unknown> = {}
  if (__setMusic) body.music = rest.music ?? null
  if (rest.locked !== undefined) body.locked = rest.locked
  return http.put<BgmAssignment>(`/api/bgm/chapters/${encodeURIComponent(stem)}`, body)
}

/** Start one mix Task per selected chapter (the no-BGM copy2 path is allowed). */
export function mixChapters(chapters: string[]): Promise<BgmBatchResult> {
  return http.post<BgmBatchResult>('/api/bgm/mix', { chapters })
}

/** Package selected finished BGM mixes into a source-named ZIP through the durable Worker. */
export function packageMixedAudio(chapters: string[]): Promise<BgmPackageResult | { task_id: string }> {
  return http.post<BgmPackageResult | { task_id: string }>('/api/bgm/package', { chapters })
}
