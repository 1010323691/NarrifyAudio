import { http, API_BASE } from './client'
import type { ApplyPreviewResult, ChapterPreviewDetail, PreviewEditInput } from '@/types'

/** 整章预览 · 章节详情：逐句剧本 + 正式 05 音频 + 暂存重渲染状态 + 下游产物状态（只读）。 */
export function previewChapter(name: string): Promise<ChapterPreviewDetail> {
  return http.get<ChapterPreviewDetail>(`/api/tts/preview/chapter/${encodeURIComponent(name)}`)
}

/** 暂存音频播放 URL（00_temp/chapter_preview 不在 files API 可寻址模块内，走专用端点；
 *  ``version`` = rendered_at 防浏览器缓存旧产物）。 */
export function previewAudioUrl(name: string, version?: string | number): string {
  const v = version ? `?v=${encodeURIComponent(String(version))}` : ''
  return `${API_BASE}/api/tts/preview/audio/${name.split('/').map(encodeURIComponent).join('/')}${v}`
}

/** 正式产物播放 URL：workspace 相对路径（如 `05_audio_chunk/<pkg>/0001.mp3` 或
 *  `06_audio_merge/<pkg>.mp3`）→ 共享 files/download 端点（``mtimeNs`` 防缓存）。 */
export function formalAudioUrl(audio: string, mtimeNs?: number | null): string {
  const [module, ...rest] = audio.split('/')
  const v = mtimeNs ? `?v=${mtimeNs}` : ''
  return `${API_BASE}/api/files/download/${module}/${rest.map(encodeURIComponent).join('/')}${v}`
}

/** 单句重渲染（tts.preview_render）：产物落暂存区，正式 05 / manifest / 03 直到 /apply 不动。
 *  只提供被修改的字段（text / speaker / instruct）。 */
export function rerenderPreviewLine(opts: {
  script: string
  index: number
  text?: string
  speaker?: string
  instruct?: string
}): Promise<{ task_id: string }> {
  const body: Record<string, unknown> = { script: opts.script, index: opts.index }
  if (opts.text !== undefined) body.text = opts.text
  if (opts.speaker !== undefined) body.speaker = opts.speaker
  if (opts.instruct !== undefined) body.instruct = opts.instruct
  return http.post<{ task_id: string }>('/api/tts/preview/line-rerender', body)
}

/** 保存修改（服务端是唯一权威门禁：逐句 staged==有效三元组 + 章节锁 + 备份回滚）。 */
export function applyPreviewEdits(script: string, edits: PreviewEditInput[]): Promise<ApplyPreviewResult> {
  return http.post<ApplyPreviewResult>('/api/tts/preview/apply', { script, edits })
}

/** 重试清理旧产物（downstream_dirty 后的「重试清理」；同样持章节锁）。 */
export function purgePreviewStale(script: string): Promise<ApplyPreviewResult> {
  return http.post<ApplyPreviewResult>('/api/tts/preview/purge-stale', { script })
}
