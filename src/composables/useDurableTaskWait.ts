import { onActivated, onDeactivated, onUnmounted, watch } from 'vue'
import { waitForDurableTask } from '@/api/persistentTasks'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'

/** Stop page-owned polling when its project view or account is disposed. */
export function useDurableTaskWait() {
  const auth = useAuthStore()
  const workspace = useProjectStore()
  let controller = new AbortController()
  watch(
    [() => auth.user?.id ?? '', () => workspace.activeProjectId],
    () => {
      controller.abort()
      controller = new AbortController()
    },
    { flush: 'sync' },
  )
  onDeactivated(() => controller.abort())
  onActivated(() => {
    if (controller.signal.aborted) controller = new AbortController()
  })
  onUnmounted(() => controller.abort())
  return (taskId: string) => waitForDurableTask(taskId, 500, controller.signal)
}
