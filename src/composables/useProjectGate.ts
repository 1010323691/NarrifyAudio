import { computed } from 'vue'
import { useSettingsStore } from '@/stores/settings'

/**
 * Global workspace gate. The pipeline stays locked until the user picks a
 * workspace folder on the dashboard (开始); this reads the config that is loaded
 * at app start, so it flips reactively as soon as the dashboard saves a new
 * workspace (or clears it).
 */
export function useProjectGate() {
  const settings = useSettingsStore()
  const projectSet = computed(() => !!(settings.config?.paths?.working_dir || '').trim())
  return { projectSet }
}
