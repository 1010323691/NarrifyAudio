import { onActivated, onDeactivated, onUnmounted, watch } from 'vue'
import { trackDurableTask, type DurableTask } from '@/api/persistentTasks'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'

/** Stop page-owned polling when its project view or account is disposed. */
export function useDurableTaskWait() {
  const auth = useAuthStore()
  const project = useProjectStore()
  let controller = new AbortController()
  watch(
    [() => auth.user?.id ?? '', () => project.activeProjectId],
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
  return {
    /** 等待持久化任务到终态。 */
    wait: (taskId: string) => trackDurableTask(taskId, () => undefined, 500, controller.signal),
    /** 轮询状态：每次快照回调 onTick；onTick 返回 false 提前停止。 */
    track: (taskId: string, onTick: (task: DurableTask) => boolean | void) =>
      trackDurableTask(taskId, onTick, 500, controller.signal),
  }
}
