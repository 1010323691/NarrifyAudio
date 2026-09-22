import { http } from './client'

export interface FileSystemDrive {
  path: string
  name: string
  kind: string
}

export interface FileSystemFolder {
  name: string
  path: string
  can_rename: boolean
  can_delete: boolean
  protected_reason?: string | null
}

export interface DirectoryListing {
  path: string
  parent_path: string | null
  can_create: boolean
  folders: FileSystemFolder[]
}

export interface FileSystemShortcut {
  path: string
  display_name: string
  created_at: string
  last_used_at: string
  exists: boolean
}

export function getDrives(): Promise<{ drives: FileSystemDrive[] }> {
  return http.get('/api/filesystem/drives')
}

export function listDirectories(path: string): Promise<DirectoryListing> {
  return http.get(`/api/filesystem/directories?path=${encodeURIComponent(path)}`)
}

export function createFolder(parent_path: string, name: string): Promise<{ path: string; name: string }> {
  return http.post('/api/filesystem/folders', { parent_path, name })
}

export function renameFolder(path: string, new_name: string): Promise<{ path: string; name: string }> {
  return http.patch('/api/filesystem/folders', { path, new_name })
}

export function deleteFolder(path: string): Promise<{ deleted: boolean; path: string }> {
  return http.del('/api/filesystem/folders', { path, recursive: true, confirmed: true })
}

export function getShortcuts(): Promise<{ shortcuts: FileSystemShortcut[] }> {
  return http.get('/api/filesystem/shortcuts')
}

export function addShortcut(path: string): Promise<FileSystemShortcut> {
  return http.post('/api/filesystem/shortcuts', { path })
}

export function removeShortcut(path: string): Promise<{ shortcuts: FileSystemShortcut[] }> {
  return http.del('/api/filesystem/shortcuts', { path })
}
