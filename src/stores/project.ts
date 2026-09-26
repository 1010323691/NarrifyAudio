import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as projectApi from '@/api/project'
import type { ProjectContext } from '@/types'
import { useSettingsStore } from '@/stores/settings'
import { usePipelineStateStore } from '@/stores/pipelineState'
import { useTaskStore } from '@/stores/task'

export const useProjectStore = defineStore('project', () => {
  const current = ref<ProjectContext | null>(null)
  const projects = ref<projectApi.ProjectSummary[]>([])
  const loaded = ref(false)
  const loading = ref(false)
  const busy = ref(false)
  const error = ref('')
  let generation = 0

  const activeProjectId = computed(() => current.value?.project_id || '')
  const activeProject = computed(() => projects.value.find((item) => item.id === activeProjectId.value) ?? null)
  const activeProjectName = computed(() => current.value?.project_name || activeProject.value?.name || '')
  const hasActiveProject = computed(() => !!current.value?.set && !!activeProjectId.value)

  function applyCurrent(value: ProjectContext) {
    const changed = (value.project_id || '') !== activeProjectId.value
    if (changed) {
      // The user task stream stays global; page selectors follow the active project.
      useTaskStore().bindProject(value.project_id || null)
      usePipelineStateStore().reset()
      useSettingsStore().reset()
    }
    current.value = value
    if (changed) void useTaskStore().refresh().catch(() => undefined)
    return changed
  }

  async function refresh() {
    const requestGeneration = generation
    loading.value = true
    error.value = ''
    const [activeResult, projectsResult] = await Promise.allSettled([
      projectApi.getActiveProject(),
      projectApi.listProjects(),
    ])
    if (requestGeneration !== generation) return current.value
    let scopeChanged = false
    if (activeResult.status === 'fulfilled') scopeChanged = applyCurrent(activeResult.value)
    else error.value = activeResult.reason?.message || '无法读取当前项目'
    if (projectsResult.status === 'fulfilled') projects.value = projectsResult.value
    else if (!error.value) error.value = projectsResult.reason?.message || '无法读取项目列表'
    loading.value = false
    loaded.value = true
    if (scopeChanged) await useSettingsStore().load()
    return current.value
  }

  async function select(projectId: string) {
    if (busy.value) return null
    generation += 1
    busy.value = true
    error.value = ''
    const requestGeneration = generation
    try {
      const selected = await projectApi.selectProject(projectId)
      if (requestGeneration !== generation) return null
      applyCurrent(selected)
      const settings = useSettingsStore()
      await settings.load()
      if (requestGeneration !== generation) return null
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
    const requestGeneration = generation
    const created = await projectApi.createProject(name)
    if (requestGeneration !== generation) return created
    await select(created.id)
    await refresh()
    return created
  }

  function setCurrent(value: ProjectContext) {
    generation += 1
    if (applyCurrent(value)) void useSettingsStore().load()
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
    useSettingsStore().reset()
  }

  return {
    current, projects, loaded, loading, busy, error,
    activeProjectId, activeProject, activeProjectName, hasActiveProject,
    refresh, select, create, setCurrent, reset,
  }
})
