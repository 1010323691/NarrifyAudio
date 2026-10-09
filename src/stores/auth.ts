import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'
import { useProjectStore } from '@/stores/project'
import { useTaskStore } from '@/stores/task'
import { useSettingsStore } from '@/stores/settings'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<authApi.AuthUser | null>(null)
  const loaded = ref(false)
  const busy = ref(false)
  const error = ref('')
  // The router guard runs on EVERY navigation; without coalescing, a stalled
  // network piles one hung /me fetch per click on top of the previous ones.
  let loadInflight: Promise<void> | null = null

  const isAuthenticated = computed(() => Boolean(user.value))

  async function load() {
    if (loaded.value) return
    loadInflight ??= (async () => {
      try {
        user.value = (await authApi.currentUser()).user
      } catch {
        user.value = null
      } finally {
        loaded.value = true
        loadInflight = null
      }
    })()
    await loadInflight
  }

  async function signIn(identifier: string, password: string) {
    busy.value = true
    error.value = ''
    try {
      const previousUserId = user.value?.id
      const signedIn = (await authApi.login({ identifier, password })).user
      if (previousUserId !== signedIn.id) {
        useProjectStore().reset()
        useTaskStore().reset()
      }
      user.value = signedIn
      await useSettingsStore().load()
    } catch (cause: any) {
      error.value = cause?.message || '登录失败'
      throw cause
    } finally {
      busy.value = false
      loaded.value = true
    }
  }

  async function signUp(email: string, password: string, username: string, displayName: string) {
    busy.value = true
    error.value = ''
    try {
      const previousUserId = user.value?.id
      const signedUp = (await authApi.register({ email, password, username, display_name: displayName })).user
      if (previousUserId !== signedUp.id) {
        useProjectStore().reset()
        useTaskStore().reset()
      }
      user.value = signedUp
      await useSettingsStore().load()
    } catch (cause: any) {
      error.value = cause?.message || '注册失败'
      throw cause
    } finally {
      busy.value = false
      loaded.value = true
    }
  }

  async function signOut() {
    await authApi.logout()
    user.value = null
    useProjectStore().reset()
    useTaskStore().reset()
  }

  return { user, loaded, busy, error, isAuthenticated, load, signIn, signUp, signOut }
})
