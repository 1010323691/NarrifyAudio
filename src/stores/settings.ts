import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getConfig, patchConfig } from '@/api/config'
import { getApplicationSettings, updateApplicationSettings } from '@/api/admin'
import type { AppConfig, DeepPartial } from '@/types'

let mediaHandler: (() => void) | null = null
let media: MediaQueryList | null = null

export const useSettingsStore = defineStore('settings', () => {
  // Project channel: 「配置随工程」 — per-project config, the values the views
  // read (working dir, ui toggles, …).
  const config = ref<AppConfig | null>(null)
  const loaded = ref(false)
  const saving = ref(false)
  let generation = 0
  let latestRequest = 0

  // Root channel: the admin platform template (backend stores it separately —
  // saving it must NOT touch the project channel; Q10). Views read only
  // `config`; `rootConfig` backs the admin settings form.
  const rootConfig = ref<AppConfig | null>(null)
  const rootLoaded = ref(false)
  const rootSaving = ref(false)
  let rootLatestRequest = 0

  function applyTheme(theme: string) {
    const root = document.documentElement
    const setDark = (dark: boolean) => root.classList.toggle('dark', dark)
    // Detach any previous system listener.
    if (media && mediaHandler) {
      media.removeEventListener('change', mediaHandler)
      media = null
      mediaHandler = null
    }
    if (theme === 'dark') setDark(true)
    else if (theme === 'light') setDark(false)
    else {
      media = window.matchMedia('(prefers-color-scheme: dark)')
      mediaHandler = () => setDark(media!.matches)
      setDark(media.matches)
      media.addEventListener('change', mediaHandler)
    }
  }

  async function load() {
    const requestGeneration = generation
    const requestId = ++latestRequest
    try {
      const value = await getConfig()
      if (requestGeneration !== generation || requestId !== latestRequest) return
      config.value = value
      applyTheme(config.value.ui.theme)
    } catch {
      /* backend may be down — leave config null */
    } finally {
      if (requestGeneration === generation && requestId === latestRequest) loaded.value = true
    }
  }

  /** Merge a partial patch (at any nesting depth) into the persisted config; returns true on success. */
  async function save(patch: DeepPartial<AppConfig>): Promise<boolean> {
    const requestGeneration = generation
    const requestId = ++latestRequest
    saving.value = true
    try {
      const value = await patchConfig(patch)
      if (requestGeneration !== generation || requestId !== latestRequest) return false
      config.value = value
      if (patch.ui?.theme) applyTheme(patch.ui.theme)
      return true
    } catch {
      return false
    } finally {
      if (requestGeneration === generation) saving.value = false
    }
  }

  /** Load the root platform config (admin channel). Never touches `config`. */
  async function loadRoot() {
    const requestId = ++rootLatestRequest
    try {
      const result = await getApplicationSettings()
      if (requestId !== rootLatestRequest) return
      rootConfig.value = result.config
    } catch {
      /* backend may be down — leave rootConfig null */
    } finally {
      if (requestId === rootLatestRequest) rootLoaded.value = true
    }
  }

  /** Persist the root platform config (admin channel); updates `rootConfig`
   *  only. The project channel (`config`) is left untouched. */
  async function saveRoot(patch: Record<string, unknown>): Promise<boolean> {
    const requestId = ++rootLatestRequest
    rootSaving.value = true
    try {
      const result = await updateApplicationSettings(patch)
      if (requestId !== rootLatestRequest) return false
      rootConfig.value = result.config
      return true
    } catch {
      return false
    } finally {
      if (requestId === rootLatestRequest) rootSaving.value = false
    }
  }

  function reset() {
    generation += 1
    rootLatestRequest += 1
    config.value = null
    loaded.value = false
    saving.value = false
    rootConfig.value = null
    rootLoaded.value = false
    rootSaving.value = false
  }

  return { config, loaded, saving, rootConfig, rootLoaded, rootSaving, load, save, loadRoot, saveRoot, applyTheme, reset }
})
