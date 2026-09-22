import { defineStore } from 'pinia'
import { ref } from 'vue'
import { health } from '@/api/client'

/** Global app state: backend connectivity (drives the sidebar status dot). */
export const useAppStore = defineStore('app', () => {
  const backendUp = ref(false)
  const lastError = ref('')

  async function ping() {
    try {
      const h = await health()
      backendUp.value = h.ok
      lastError.value = ''
    } catch (e: any) {
      backendUp.value = false
      lastError.value = e?.message || '无法连接后端'
    }
  }

  // The store is an app-lifetime singleton — the interval never needs clearing.
  window.setInterval(ping, 3000)
  ping()

  return { backendUp, lastError, ping }
})
