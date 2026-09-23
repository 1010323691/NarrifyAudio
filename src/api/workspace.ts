import { http } from './client'
import type { WorkspaceInfo } from '@/types'

export interface RecentWorkspace {
  path: string
  workspace_id?: string
  name?: string
  display_name?: string
  last_used_at?: string
  created_at?: string
  exists: boolean
  is_current: boolean
}

export interface ManagedProject {
  id: string
  name: string
  directory_key: string
  created_at: string
  updated_at: string
}

export interface WorkspaceFileSummary {
  name: string
  relative_path: string
  module: string
  size_bytes: number
  modified_at: string
}

export interface WorkspaceSummary {
  workspace_id: string
  name: string
  updated_at: string
  file_count: number
  size_bytes: number
  categories: { key: string; label: string; count: number; size_bytes: number }[]
  recent_files: WorkspaceFileSummary[]
  recent_outputs: WorkspaceFileSummary[]
  cleanup_candidates: { count: number; size_bytes: number; older_than_days: number; blocked_by_active_tasks: boolean }
}

/** Current workspace + its artifact directories (empty path/dirs when unset). */
export function getWorkspace(): Promise<WorkspaceInfo> {
  return http.get<WorkspaceInfo>('/api/workspace')
}

/**
 * Set the pipeline's workspace folder (creating its subdirectories); an empty
 * path clears it, which re-locks the pipeline. Existing files are never moved
 * or deleted.
 */
export function setWorkspace(path: string): Promise<WorkspaceInfo> {
  return http.put<WorkspaceInfo>('/api/workspace', { path })
}

export function selectWorkspace(workspaceId: string): Promise<WorkspaceInfo> {
  return http.put<WorkspaceInfo>('/api/workspace', { workspace_id: workspaceId })
}

export function createManagedWorkspace(name: string): Promise<{ id: string; name: string; directory_key: string }> {
  return http.post('/api/v1/workspaces', { name })
}

export function listManagedProjects(): Promise<ManagedProject[]> {
  return http.get('/api/v1/workspaces')
}

export function getWorkspaceSummary(workspaceId: string): Promise<WorkspaceSummary> {
  return http.get(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/summary`)
}

export function cleanupWorkspaceTemp(workspaceId: string): Promise<{ deleted_count: number; deleted_bytes: number; skipped_count: number; older_than_days: number }> {
  return http.post(`/api/v1/workspaces/${encodeURIComponent(workspaceId)}/cleanup-temp`, {})
}

export function getRecentWorkspaces(): Promise<{ workspaces: RecentWorkspace[] }> {
  return http.get('/api/workspace/recent')
}

export function removeRecentWorkspace(path: string): Promise<{ workspaces: RecentWorkspace[] }> {
  return http.del('/api/workspace/recent', { path })
}
