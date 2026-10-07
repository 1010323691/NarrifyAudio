import { listQuery, type ListQuery, type ListPagination } from '@/api/listPaging'
import { http } from './client'
import type { UploadResult, DirListResult } from '@/types'

/**
 * File selection: upload a chosen file into the backend's ``01_input/`` directory
 * and get its absolute path back. This is the file-input path — the UI's hidden
 * ``<input type=file>`` hands the picked File here (see ``utils/fileops.ts``).
 */
export async function uploadFile(file: File): Promise<UploadResult> {
  const form = new FormData()
  form.append('file', file)
  form.append('filename', file.name)
  return http.upload<UploadResult>('/api/files/upload', form)
}

/**
 * List the entries of a workspace pipeline directory (``GET /api/files/list/{module}``).
 * ``module`` is the directory name (e.g. ``02_split_text``). Used by the parse page to
 * enumerate the split files the user can select — the backend does the reading.
 */
export function listDir(module: string, recursive = false, options?: Partial<ListQuery>, signal?: AbortSignal, filters: { extensions?: string; exclude_suffix?: string; kind?: string } = {}): Promise<DirListResult & { pagination?: ListPagination }> {
  return http.get(`/api/files/list/${encodeURIComponent(module)}?${listQuery(options, { recursive, ...filters })}`, { signal })
}
