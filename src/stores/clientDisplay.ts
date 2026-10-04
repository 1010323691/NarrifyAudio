import { defineStore } from 'pinia'
import { ref } from 'vue'
import { http } from '@/api/client'

export const useClientDisplayStore = defineStore('clientDisplay', () => {
  const logsEnabled = ref(false)
  const loaded = ref(false)
  const saving = ref(false)
  let revision = 0

  async function load() {
    if (saving.value) return
    const request = ++revision
    try {
      const result = await http.get<{ enabled: boolean }>('/api/config/client-logs')
      if (request !== revision) return
      logsEnabled.value = result.enabled === true
      loaded.value = true
    } catch {
      if (request === revision) logsEnabled.value = false
    }
  }

  async function save(enabled: boolean) {
    const request = ++revision
    saving.value = true
    try {
      const result = await http.patch<{ enabled: boolean }>('/api/v1/admin/settings/client-logs', { enabled })
      if (request !== revision) return false
      logsEnabled.value = result.enabled === true
      loaded.value = true
      return true
    } finally {
      if (request === revision) saving.value = false
    }
  }

  function reset() {
    ++revision
    logsEnabled.value = false
    loaded.value = false
    saving.value = false
  }

  return { logsEnabled, loaded, saving, load, save, reset }
})
