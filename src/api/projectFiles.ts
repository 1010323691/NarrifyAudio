import { http } from './client'

export interface ProjectFile {
  id: string
  name: string
  module: string | null
  kind: string
  content_type: string
  size_bytes: number
  sha256: string
  created_at: string
}

export function listProjectFiles(projectId: string): Promise<ProjectFile[]> {
  return http.get(`/api/v1/projects/${encodeURIComponent(projectId)}/files`)
}

