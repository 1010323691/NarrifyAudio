import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getConfig, patchConfig } from '@/api/config'
import type { AppConfig, DeepPartial } from '@/types'

let mediaHandler: (() => void) | null = null
let media: MediaQueryList | null = null

export const useSettingsStore = defineStore('settings', () => {
  const config = ref<AppConfig | null>(null)
  const loaded = ref(false)
  const saving = ref(false)
  let generation = 0
  let latestRequest = 0

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

  function reset() {
    generation += 1
    config.value = null
    loaded.value = false
    saving.value = false
  }

  return { config, loaded, saving, load, save, applyTheme, reset }
})
