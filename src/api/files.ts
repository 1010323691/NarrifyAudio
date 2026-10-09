import { http } from './client'
import type { UploadResult } from '@/types'

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
