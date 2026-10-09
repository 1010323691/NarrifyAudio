// Per-view loading state for the admin console.
//
// Carries over the old single-file console's guarantees, now per view:
// a stale response can never overwrite a newer one (epoch ticket + abort),
// leaving a cached (keep-alive) view invalidates its in-flight read, a slow
// poll never stacks a second request, and a mutation blocks external reads
// so a read begun before it cannot restore the pre-mutation state.
import { onActivated, onBeforeUnmount, onDeactivated, onMounted, ref, watch } from 'vue'

const INTERVAL_KEY = 'narrify.admin.refreshInterval'
const INTERVALS = [0, 15000, 30000, 60000]
function readInterval(): number {
  try {
    const raw = globalThis.localStorage?.getItem(INTERVAL_KEY)
    return raw != null && INTERVALS.includes(Number(raw)) ? Number(raw) : 15000
  } catch { return 15000 }
}
/** Auto-refresh preference shared by every admin view (0 = off). */
export const refreshInterval = ref(readInterval())
export const refreshIntervalOptions = INTERVALS
watch(refreshInterval, value => {
  try { globalThis.localStorage?.setItem(INTERVAL_KEY, String(value)) } catch { /* private mode */ }
})

export function errorMessage(cause: unknown): string {
  return (cause as { message?: string } | null)?.message || String(cause)
}

/**
 * `fetcher` performs the reads and returns a closure that applies them; the
 * closure only runs when no newer load (or deactivation/mutation) superseded it.
 */
export function useAdminLoader(fetcher: (signal: AbortSignal) => Promise<() => void>, options: {
  poll?: boolean
  /** Called with the failure message when a mutation leaves an error behind (toast). */
  onActionError?: (message: string) => void
} = {}) {
  const loading = ref(false)
  const error = ref('')
  const loaded = ref(false)
  const lastUpdated = ref('')
  const actionBusy = ref(false)
  let epoch = 0
  let active = true
  let abort: AbortController | null = null
  let timer: ReturnType<typeof setInterval> | null = null

  async function loadData() {
    const ticket = ++epoch
    abort?.abort()
    abort = new AbortController()
    loading.value = true
    error.value = ''
    try {
      const apply = await fetcher(abort.signal)
      if (ticket !== epoch) return
      apply()
      loaded.value = true
      lastUpdated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    } catch (cause) {
      if (ticket === epoch) error.value = errorMessage(cause)
    } finally {
      if (ticket === epoch) loading.value = false
    }
  }

  async function load() {
    if (actionBusy.value) return
    await loadData()
  }

  /** Mutation wrapper: one at a time, invalidates in-flight reads, refreshes afterwards if needed. */
  async function runAction(action: () => Promise<void>) {
    if (actionBusy.value) return
    const resumeLoad = loading.value
    actionBusy.value = true
    epoch++
    loading.value = false
    error.value = ''
    try {
      await action()
    } catch (cause) {
      error.value = errorMessage(cause)
    } finally {
      if (error.value) options.onActionError?.(error.value)
      actionBusy.value = false
      if (active && (resumeLoad || !loaded.value)) await load()
    }
  }

  function restartTimer() {
    if (timer) clearInterval(timer)
    timer = null
    if (!options.poll || !refreshInterval.value) return
    timer = setInterval(() => {
      if (active && !loading.value && !actionBusy.value && globalThis.document?.visibilityState === 'visible') void load()
    }, refreshInterval.value)
  }

  if (options.poll) watch(refreshInterval, restartTimer)
  onMounted(() => { void load(); restartTimer() })
  onActivated(() => { if (!active) { active = true; void load() } })
  onDeactivated(() => { abort?.abort(); active = false; epoch++; loading.value = false })
  onBeforeUnmount(() => { abort?.abort(); epoch++; if (timer) clearInterval(timer) })

  return { loading, error, loaded, lastUpdated, actionBusy, load, loadData, runAction, poll: !!options.poll }
}

export type AdminLoaderHandle = ReturnType<typeof useAdminLoader>
