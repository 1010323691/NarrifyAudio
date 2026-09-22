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

export function getRecentWorkspaces(): Promise<{ workspaces: RecentWorkspace[] }> {
  return http.get('/api/workspace/recent')
}

export function removeRecentWorkspace(path: string): Promise<{ workspaces: RecentWorkspace[] }> {
  return http.del('/api/workspace/recent', { path })
}
