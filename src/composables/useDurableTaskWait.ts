import { onActivated, onDeactivated, onUnmounted } from 'vue'
import { waitForDurableTask } from '@/api/persistentTasks'

/** Stop page-owned polling when its project view or account is disposed. */
export function useDurableTaskWait() {
  let controller = new AbortController()
  onDeactivated(() => controller.abort())
  onActivated(() => {
    if (controller.signal.aborted) controller = new AbortController()
  })
  onUnmounted(() => controller.abort())
  return (taskId: string) => waitForDurableTask(taskId, 500, controller.signal)
}
