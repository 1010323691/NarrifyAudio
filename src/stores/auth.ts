import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<authApi.AuthUser | null>(null)
  const loaded = ref(false)
  const busy = ref(false)
  const error = ref('')

  const isAuthenticated = computed(() => Boolean(user.value))

  async function load() {
    if (loaded.value) return
    try {
      user.value = (await authApi.currentUser()).user
    } catch {
      user.value = null
    } finally {
      loaded.value = true
    }
  }

  async function signIn(email: string, password: string) {
    busy.value = true
    error.value = ''
    try {
      user.value = (await authApi.login({ email, password })).user
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
      user.value = (await authApi.register({ email, password, username, display_name: displayName })).user
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
  }

  return { user, loaded, busy, error, isAuthenticated, load, signIn, signUp, signOut }
})
