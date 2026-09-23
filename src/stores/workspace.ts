import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as workspaceApi from '@/api/workspace'
import type { WorkspaceInfo } from '@/types'
import { useSettingsStore } from '@/stores/settings'

export const useWorkspaceStore = defineStore('workspace', () => {
  const current = ref<WorkspaceInfo | null>(null)
  const projects = ref<workspaceApi.ManagedProject[]>([])
  const loaded = ref(false)
  const loading = ref(false)
  const busy = ref(false)
  const error = ref('')

  const activeProjectId = computed(() => current.value?.workspace_id || current.value?.project_id || '')
  const activeProject = computed(() => projects.value.find((item) => item.id === activeProjectId.value) ?? null)
  const activeProjectName = computed(() => current.value?.workspace_name || activeProject.value?.name || '')
  const hasActiveProject = computed(() => !!current.value?.set && !!activeProjectId.value)

  async function refresh() {
    loading.value = true
    error.value = ''
    const [activeResult, projectsResult] = await Promise.allSettled([
      workspaceApi.getWorkspace(),
      workspaceApi.listManagedProjects(),
    ])
    if (activeResult.status === 'fulfilled') current.value = activeResult.value
    else error.value = activeResult.reason?.message || '无法读取当前项目'
    if (projectsResult.status === 'fulfilled') projects.value = projectsResult.value
    else if (!error.value) error.value = projectsResult.reason?.message || '无法读取项目列表'
    loading.value = false
    loaded.value = true
    return current.value
  }

  async function select(projectId: string) {
    if (busy.value) return null
    busy.value = true
    error.value = ''
    try {
      current.value = await workspaceApi.selectWorkspace(projectId)
      const settings = useSettingsStore()
      await settings.load()
      return current.value
    } catch (cause: any) {
      error.value = cause?.message || '无法打开此项目'
      throw cause
    } finally {
      busy.value = false
      loaded.value = true
    }
  }

  async function create(name: string) {
    const created = await workspaceApi.createManagedWorkspace(name)
    await select(created.id)
    await refresh()
    return created
  }

  function setCurrent(value: WorkspaceInfo) {
    current.value = value
    loaded.value = true
  }

  return {
    current, projects, loaded, loading, busy, error,
    activeProjectId, activeProject, activeProjectName, hasActiveProject,
    refresh, select, create, setCurrent,
  }
})
