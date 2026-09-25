import { taskModuleLabel } from '@/utils/taskTypes'

/** Human-readable task labels for user-facing project views. */
export function taskTypeLabel(taskType: string): string {
  return taskModuleLabel(taskType)
}
