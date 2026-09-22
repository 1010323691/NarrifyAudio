import { http } from './client'
import type { AppConfig, DeepPartial } from '@/types'

/** Read the full unified config. */
export function getConfig(): Promise<AppConfig> {
  return http.get<AppConfig>('/api/config')
}

/** Merge a (possibly partial, at any nesting depth) patch into the config and persist it.
 *  The backend ``update_config`` deep-merges, so callers may send just the fields that
 *  changed — e.g. ``{ tts: { batch_concurrency } }`` — rather than the whole ``AppConfig``. */
export function patchConfig(patch: DeepPartial<AppConfig>): Promise<AppConfig> {
  return http.put<AppConfig>('/api/config', patch)
}
