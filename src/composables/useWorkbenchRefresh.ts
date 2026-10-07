import { onBeforeUnmount, onDeactivated, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { useWorkbenchScope } from './useWorkbenchScope'

/** Coalesce task bursts and serialize expensive file/status reads. */
export function useWorkbenchRefresh(refresh: () => Promise<unknown>, delay: number) {
  const auth = useAuthStore()
  const project = useProjectStore()
  const captureScope = useWorkbenchScope()
  let timer: ReturnType<typeof setTimeout> | null = null
  let running = false
  let pending = false
  function schedule() {
    pending = true
    if (timer || running) return
    const isCurrent = captureScope()
    timer = setTimeout(async () => {
      timer = null
      pending = false
      if (!isCurrent()) return
      running = true
      try { await refresh() }
      finally {
        running = false
        if (pending) schedule()
      }
    }, delay)
  }
  function stop() {
    if (timer) clearTimeout(timer)
    timer = null
    pending = false
  }
  watch([() => auth.user?.id, () => project.activeProjectId], stop, { flush: 'sync' })
  onDeactivated(stop)
  onBeforeUnmount(stop)
  return { schedule, stop }
}
