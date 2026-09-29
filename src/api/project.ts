import { http } from './client'
import type { ProjectContext } from '@/types'

export interface ProjectSummary {
  id: string
  name: string
  directory_key: string
  created_at: string
  updated_at: string
}

export interface TrashedProjectSummary extends ProjectSummary {
  deleted_at: string
  expires_at: string
}

export interface ProjectFileSummary {
  name: string
  relative_path: string
  module: string
  size_bytes: number
  modified_at: string
}

/** One tracked artifact row (``GET /api/v1/projects/{id}/files``) — carries the live content sha. */
export interface ProjectFileRecord {
  id: string
  name: string
  module: string | null
  kind: string
  content_type: string
  size_bytes: number
  sha256: string
  created_at: string
}

export interface ProjectStorageSummary {
  project_id: string
  name: string
  updated_at: string
  file_count: number
  size_bytes: number
  split_volume_count: number
  categories: { key: string; label: string; count: number; size_bytes: number }[]
  recent_files: ProjectFileSummary[]
  recent_outputs: ProjectFileSummary[]
  cleanup_candidates: { count: number; size_bytes: number; older_than_days: number; blocked_by_active_tasks: boolean }
}

export interface ProjectProgressSummary {
  project_id: string
  name: string
  updated_at: string
  stage_keys: string[]
  split_volume_count: number
}

/** Active project and its artifact directories (empty when no project is selected). */
export function getActiveProject(): Promise<ProjectContext> {
  return http.get<ProjectContext>('/api/v1/projects/active')
}

/** Select or clear the current project without moving or deleting its files. */
export function selectProject(projectId: string | null): Promise<ProjectContext> {
  return http.put<ProjectContext>('/api/v1/projects/active', { project_id: projectId })
}

export function createProject(name: string): Promise<ProjectSummary> {
  return http.post('/api/v1/projects', { name })
}

export function listProjects(): Promise<ProjectSummary[]> {
  return http.get('/api/v1/projects')
}

export function listTrashedProjects(): Promise<TrashedProjectSummary[]> {
  return http.get('/api/v1/projects/trash')
}

export function restoreProject(projectId: string): Promise<ProjectSummary> {
  return http.post(`/api/v1/projects/${encodeURIComponent(projectId)}/restore`, {})
}

export function deleteProject(projectId: string): Promise<{ ok: boolean }> {
  return http.del(`/api/v1/projects/${encodeURIComponent(projectId)}`)
}

export function getProjectSummary(projectId: string): Promise<ProjectStorageSummary> {
  return http.get(`/api/v1/projects/${encodeURIComponent(projectId)}/summary`)
}

/** Tracked files of a project with live sha256 — used to detect stale parse inputs. */
export function listProjectFiles(projectId: string): Promise<ProjectFileRecord[]> {
  return http.get(`/api/v1/projects/${encodeURIComponent(projectId)}/files`)
}

export function getProjectProgressSummary(projectId: string): Promise<ProjectProgressSummary> {
  return http.get(`/api/v1/projects/${encodeURIComponent(projectId)}/summary?progress=true`)
}

export function cleanupProjectTemp(projectId: string): Promise<{ deleted_count: number; deleted_bytes: number; skipped_count: number; older_than_days: number }> {
  return http.post(`/api/v1/projects/${encodeURIComponent(projectId)}/cleanup-temp`, {})
}
