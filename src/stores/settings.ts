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
    try {
      config.value = await getConfig()
      applyTheme(config.value.ui.theme)
    } catch {
      /* backend may be down — leave config null */
    } finally {
      loaded.value = true
    }
  }

  /** Merge a partial patch (at any nesting depth) into the persisted config; returns true on success. */
  async function save(patch: DeepPartial<AppConfig>): Promise<boolean> {
    saving.value = true
    try {
      config.value = await patchConfig(patch)
      if (patch.ui?.theme) applyTheme(patch.ui.theme)
      return true
    } catch {
      return false
    } finally {
      saving.value = false
    }
  }

  return { config, loaded, saving, load, save, applyTheme }
})
