// Music library API (全局音乐库；后端 /api/music)。
// 瘦客户端：只做 HTTP 封装，无处理逻辑。

import { API_BASE, http } from './client'
import type {
  MusicLibrary,
  MusicTrack,
  MusicDeleteResult,
  MusicTagCategory,
  TrackTags,
  SuggestBatchResult,
  ApplySuggestionsResult,
} from '@/types'

/** The full index (tag registry + all tracks). */
export function getLibrary(): Promise<MusicLibrary> {
  return http.get<MusicLibrary>('/api/music/library')
}

/** Preview (audio) URL for a library track — the library is NOT served via the
 *  workspace files API. */
export function musicPreviewUrl(name: string): string {
  return `${API_BASE}/api/music/preview/${encodeURIComponent(name)}`
}

/** Upload one music file (mp3/wav/flac). Same name -> ApiError 409 (never overwrites).
 *  ``folder`` (optional) = the folder the file permanently belongs to
 *  ("" / omitted = 未分类, library root). The file itself always lands flat
 *  at the library root — folders are index metadata. */
export async function uploadMusic(file: File, folder?: string): Promise<{ name: string; track: MusicTrack }> {
  const form = new FormData()
  form.append('file', file)
  if (folder) form.append('folder', folder)
  return http.upload<{ name: string; track: MusicTrack }>('/api/music/upload', form)
}

/** Update a track's tags / enabled / description (partial). */
export function updateTrack(
  name: string,
  patch: { tags?: TrackTags; enabled?: boolean; description?: string },
): Promise<{ name: string; track: MusicTrack }> {
  return http.put<{ name: string; track: MusicTrack }>(
    `/api/music/tracks/${encodeURIComponent(name)}`,
    patch,
  )
}

/** Delete one track. Locked chapter references skip it (see `skipped`). */
export function deleteTrack(name: string): Promise<MusicDeleteResult> {
  return http.del<MusicDeleteResult>(`/api/music/tracks/${encodeURIComponent(name)}`)
}

/** Batch enable/disable. */
export function batchEnable(names: string[], enabled: boolean): Promise<{ updated: number; missing: string[] }> {
  return http.post('/api/music/tracks/batch-enable', { names, enabled })
}

/** Batch add/remove tag names on the SELECTED tracks. */
export function batchTags(
  tracks: string[],
  tagNames: string[],
  category: MusicTagCategory,
  op: 'add' | 'remove',
): Promise<{ updated: number; missing: string[] }> {
  return http.post('/api/music/tracks/batch-tags', { tracks, names: tagNames, category, op })
}

/** Batch delete (same locked-reference skip semantics as single delete). */
export function batchDelete(names: string[]): Promise<MusicDeleteResult> {
  return http.post<MusicDeleteResult>('/api/music/tracks/batch-delete', { names })
}

/** Create a folder (index metadata only). Duplicate name -> ApiError 409. */
export function createFolder(name: string): Promise<{ folder: string; folders: Record<string, { created_at: string }> }> {
  return http.post('/api/music/folders', { name })
}

/** Rename a folder + propagate to every track's folder field (single
 *  transaction). 404 source missing / 409 target taken. */
export function renameFolder(name: string, newName: string): Promise<{ folder: string; renamed_tracks: number; folders: Record<string, { created_at: string }> }> {
  return http.put(`/api/music/folders/${encodeURIComponent(name)}`, { new_name: newName })
}

/** Delete a folder. Non-empty -> ApiError 409 (tracks must be moved out first
 *  — music files are never deleted). */
export function deleteFolder(name: string): Promise<{ deleted: string[]; folders: Record<string, { created_at: string }> }> {
  return http.del(`/api/music/folders/${encodeURIComponent(name)}`)
}

/** Move tracks to a folder (single or batch; ``folder = ""`` = 未分类 / root).
 *  Target folder missing -> ApiError 404; missing names are reported, not an error. */
export function moveTracks(names: string[], folder: string): Promise<{ moved: string[]; missing: string[]; folder: string }> {
  return http.post('/api/music/tracks/move', { names, folder })
}

/** Add a tag to the registry (global-uniqueness 409 on clash). */
export function createTag(category: MusicTagCategory, name: string): Promise<{ tags: Record<MusicTagCategory, string[]> }> {
  return http.post('/api/music/tags', { category, name })
}

/** Rename a registry tag (propagates to all tracks + the analysis cache). */
export function renameTag(
  category: MusicTagCategory,
  name: string,
  newName: string,
): Promise<{ tags: Record<MusicTagCategory, string[]>; affected_tracks: number }> {
  return http.post('/api/music/tags/rename', { category, name, new_name: newName })
}

/** Remove a tag from registry + all tracks (never deletes music files). */
export function deleteTag(category: MusicTagCategory, name: string): Promise<{ tags: Record<MusicTagCategory, string[]>; deleted_track_refs: number }> {
  return http.del(`/api/music/tags/${encodeURIComponent(category)}/${encodeURIComponent(name)}`)
}

/** AI-recommended tags (filename + description + vocabulary; the LLM never
 *  reads the audio). Results are in-vocabulary candidates for user confirmation. */
export function suggestTagsDurable(name: string, description?: string): Promise<{ task_id: string }> {
  return http.post<{ task_id: string }>('/api/music/suggest-tags', { name, description })
}

/** One-click batch AI recognition: one Task per selected track (shared LLM
 *  gate, per-track progress/cancel). Candidates land in the suggestions
 *  cache — nothing is applied to track tags until the user confirms. */
export function suggestTagsBatch(names: string[]): Promise<SuggestBatchResult> {
  return http.post<SuggestBatchResult>('/api/music/suggest-tags-batch', { names })
}

/** Adopt cached AI candidates (the「AI 推荐采用」confirmation): only tracks
 *  with a candidate AND no manual tags are touched — a user decision is never
 *  overwritten; applied tracks consume their candidate. */
export function applySuggestions(names: string[]): Promise<ApplySuggestionsResult> {
  return http.post<ApplySuggestionsResult>('/api/music/tracks/apply-suggestions', { names })
}
