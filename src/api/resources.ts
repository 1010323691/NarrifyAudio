import { listQuery, type ListPagination } from '@/api/listPaging'
import { API_BASE, http } from './client'

export interface ResourceCategory {
  key: string
  label: string
  count: number
  size_bytes: number
}
export interface ResourceSnapshot {
  snapshot_id: string
  scanned_at: string
  complete: boolean
  errors: Array<{ path: string; message: string }>
  file_count: number
  size_bytes: number
  categories: ResourceCategory[]
  audio_count: number
  latest_modified_at: string | null
}
export interface ResourceProject {
  delivery_count: number
  delivery_bytes: number
  production_audio_count: number
  production_categories: ResourceCategory[]
  project_id: string
  name: string
  snapshot: ResourceSnapshot | null
  scan_task_id: string | null
  scan_status: string | null
  scan_error: string | null
  stale: boolean
}
export interface ResourceExport {
  unavailable_reason?: string | null
  task_id: string
  name: string
  file_count: number
  size_bytes: number
  stored_bytes: number
  expires_at: string
  available: boolean
}
export interface ResourceOverview {
  pagination?: ListPagination
  projects: ResourceProject[]
  categories: ResourceCategory[]
  storage: { project_bytes: number; project_complete: boolean; trash_bytes: number; trash_complete: boolean; export_bytes: number }
  exports: ResourceExport[]
}
export interface ResourceEntry {
  id: string
  project_id: string
  project_name: string
  name: string
  relative_path: string
  module: string
  module_label: string
  kind: 'file' | 'directory'
  size_bytes: number
  modified_at: string
  extension: string
  preview_kind: 'audio' | 'image' | 'text' | 'json' | 'config' | null
  snapshot_id: string
  can_preview: boolean
  can_download: boolean
  can_package: boolean
  resource_role: 'deliverable' | 'production'
  delivery_label: string | null
  completed_at: string | null
  delivery_version: string | null
  download_reason: string | null
}
export interface SnapshotReference { project_id: string; snapshot_id: string }
export interface ResourceEntries {
  items: ResourceEntry[]
  total: number
  size_bytes: number
  page: number
  page_size: number
  snapshots: SnapshotReference[]
  complete: boolean
  incomplete_projects: string[]
}
export interface ResourceQuery {
  role?: 'deliverable' | 'production'
  project_id?: string
  category?: string
  path?: string
  query?: string
  extension?: string
  sort?: 'name' | 'modified' | 'size'
  page?: number
  page_size?: number
  directory_mode?: boolean
}
export interface ResourcePreview { file: ResourceEntry; content: string | null; truncated: boolean; encoding?: string; warning: string | null }
export interface CleanupProject {
  project_id: string
  name: string
  count: number
  size_bytes: number
  blocked: boolean
  complete: boolean
  snapshot_id: string | null
  directories: Array<{ module: string; count: number; size_bytes: number }>
}
export interface CleanupPreview {
  projects: CleanupProject[]
  items: Array<{ id: string; project_id: string; project_name: string; relative_path: string; size_bytes: number }>
  total: number
  older_than_days: number
}

function queryString(params: Record<string, unknown>): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') query.set(key, String(value))
  }
  return query.toString()
}
export function getResourceOverview(options?: RequestInit, query?: { page: number; page_size: number; q: string; sort: string; project_id?: string }): Promise<ResourceOverview> {
  return http.get(`/api/v1/resources?${listQuery(undefined, query)}`, options)
}
export function getResourceProjectIds(options?: RequestInit): Promise<{ project_ids: string[] }> {
  return http.get('/api/v1/resources?keys_only=true', options)
}
export function getResourceEntries(query: ResourceQuery, options?: RequestInit): Promise<ResourceEntries> {
  return http.get(`/api/v1/resources/entries?${queryString({ ...query })}`, options)
}
export function getResourcePreview(id: string, options?: RequestInit): Promise<ResourcePreview> {
  return http.get(`/api/v1/resources/files/${encodeURIComponent(id)}/preview`, options)
}
export function resourceFileUrl(id: string, mode: 'preview' | 'download' = 'download'): string {
  return `${API_BASE}/api/v1/resources/files/${encodeURIComponent(id)}/${mode}`
}
export function resourceExportUrl(id: string): string {
  return `${API_BASE}/api/v1/resources/exports/${encodeURIComponent(id)}/download`
}
export function getCleanupPreview(projectIds: string[], page = 1, options?: RequestInit): Promise<CleanupPreview> {
  const query = new URLSearchParams({ page: String(page) })
  for (const id of projectIds) query.append('project_ids', id)
  return http.get(`/api/v1/resources/cleanup-preview?${query}`, options)
}
export function submitResourceTask(projectId: string, taskType: 'resources.scan' | 'resources.package' | 'resources.cleanup', payload: Record<string, unknown>): Promise<{ id: string; status: string }> {
  return http.post('/api/v1/tasks', {
    project_id: projectId, task_type: taskType, payload,
    idempotency_key: `resources-${crypto.randomUUID()}`,
  })
}
