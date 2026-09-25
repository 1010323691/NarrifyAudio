import { computed } from 'vue'
import { useSettingsStore } from '@/stores/settings'

/**
 * Global project gate. The pipeline stays locked until an active project is
 * set on the dashboard (项目); this reads the config that is loaded at app
 * start, so it flips reactively as soon as the dashboard activates a project
 * (or the active project is cleared).
 */
export function useProjectGate() {
  const settings = useSettingsStore()
  const projectSet = computed(() => !!(settings.config?.paths?.working_dir || '').trim())
  return { projectSet }
}
