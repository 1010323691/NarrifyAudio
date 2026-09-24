import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as workspaceApi from '@/api/workspace'
import type { WorkspaceInfo } from '@/types'
import { useSettingsStore } from '@/stores/settings'
import { usePipelineStateStore } from '@/stores/pipelineState'

export const useWorkspaceStore = defineStore('workspace', () => {
  const current = ref<WorkspaceInfo | null>(null)
  const projects = ref<workspaceApi.ManagedProject[]>([])
  const loaded = ref(false)
  const loading = ref(false)
  const busy = ref(false)
  const error = ref('')
  let generation = 0

  const activeProjectId = computed(() => current.value?.workspace_id || current.value?.project_id || '')
  const activeProject = computed(() => projects.value.find((item) => item.id === activeProjectId.value) ?? null)
  const activeProjectName = computed(() => current.value?.workspace_name || activeProject.value?.name || '')
  const hasActiveProject = computed(() => !!current.value?.set && !!activeProjectId.value)

  async function refresh() {
    const requestGeneration = generation
    loading.value = true
    error.value = ''
    const [activeResult, projectsResult] = await Promise.allSettled([
      workspaceApi.getWorkspace(),
      workspaceApi.listManagedProjects(),
    ])
    if (requestGeneration !== generation) return current.value
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
    generation += 1
    busy.value = true
    error.value = ''
    const requestGeneration = generation
    try {
      const previousProjectId = activeProjectId.value
      const selected = await workspaceApi.selectWorkspace(projectId)
      if (requestGeneration !== generation) return null
      current.value = selected
      if (activeProjectId.value !== previousProjectId) usePipelineStateStore().reset()
      const settings = useSettingsStore()
      await settings.load()
      return current.value
    } catch (cause: any) {
      if (requestGeneration === generation) error.value = cause?.message || '无法打开此项目'
      throw cause
    } finally {
      if (requestGeneration === generation) {
        busy.value = false
        loading.value = false
        loaded.value = true
      }
    }
  }

  async function create(name: string) {
    const created = await workspaceApi.createManagedWorkspace(name)
    await select(created.id)
    await refresh()
    return created
  }

  function setCurrent(value: WorkspaceInfo) {
    generation += 1
    if ((value.workspace_id || value.project_id || '') !== activeProjectId.value) usePipelineStateStore().reset()
    current.value = value
    loading.value = false
    loaded.value = true
  }

  function reset() {
    generation += 1
    current.value = null
    projects.value = []
    loaded.value = false
    loading.value = false
    busy.value = false
    error.value = ''
    usePipelineStateStore().reset()
  }

  return {
    current, projects, loaded, loading, busy, error,
    activeProjectId, activeProject, activeProjectName, hasActiveProject,
    refresh, select, create, setCurrent, reset,
  }
})
