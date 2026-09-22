import { http } from './client'
import type { TextFormatResult, TextToggles } from '@/types'

/** Format a TXT file. ``config`` is a partial override for the 10 toggles;
 *  any key omitted falls back to the backend's persisted text config. */
export function formatText(path: string, config?: Partial<TextToggles>): Promise<TextFormatResult> {
  return http.post<TextFormatResult>('/api/text/format', { path, config: config ?? {} })
}
