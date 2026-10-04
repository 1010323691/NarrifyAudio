import { onActivated, onDeactivated, onBeforeUnmount, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'

export async function withinScope<T>(promise: Promise<T>, isCurrent: () => boolean): Promise<T> {
  const value = await promise
  if (!isCurrent()) throw new DOMException('工作台上下文已切换', 'AbortError')
  return value
}

/** A continuation may only update the page which initiated it. */
export function useWorkbenchScope() {
  const auth = useAuthStore()
  const project = useProjectStore()
  let generation = 0
  let active = true
  watch(
    [() => auth.user?.id, () => project.activeProjectId],
    () => {
      generation++
    },
    { flush: 'sync' },
  )
  onActivated(() => {
    active = true
  })
  onDeactivated(() => {
    active = false
    generation++
  })
  onBeforeUnmount(() => {
    active = false
    generation++
  })
  return () => {
    const captured = generation
    const user = auth.user?.id
    const projectId = project.activeProjectId
    return () =>
      active &&
      captured === generation &&
      user === auth.user?.id &&
      projectId === project.activeProjectId
  }
}
