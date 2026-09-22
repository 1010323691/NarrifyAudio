// Browser file bridge: pick a file via a hidden <input type=file> and upload it
// into the backend's 01_input/ dir (whose absolute path is handed back to the
// backend), and deliver outputs via the download endpoint, which replies
// Content-Disposition: attachment so the browser saves it to Downloads.

import { API_BASE } from '@/api/client'
import { uploadFile } from '@/api/files'

export interface PickedFile {
  path: string
  name: string
  size: number
}

export interface FileFilter {
  name: string
  /** Extensions without dots, e.g. ['txt'] / ['mp3','wav','m4a']. */
  extensions: string[]
}

/**
 * Pick a file. A hidden <input type=file> lets the user choose; the file is then
 * uploaded into the backend's ``01_input/`` dir and its absolute path is returned
 * so the caller can hand it straight to the backend. Resolves to null if the user
 * cancels or the upload fails.
 */
export async function pickFile(filters: FileFilter[] = []): Promise<PickedFile | null> {
  return new Promise<PickedFile | null>((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    if (filters.length) {
      input.accept = filters.flatMap((f) => f.extensions.map((e) => `.${e}`)).join(',')
    }
    input.onchange = async () => {
      const file = input.files?.[0]
      if (!file) {
        resolve(null)
        return
      }
      try {
        const r = await uploadFile(file)
        resolve({ path: r.path, name: r.name, size: r.size })
      } catch (e) {
        console.error('上传失败', e)
        resolve(null)
      }
    }
    input.click()
  })
}

/**
 * Pick MULTIPLE files (no upload — the caller decides where each File goes;
 * used by the 音乐库 bulk upload, which uploads per-file for 409/400 semantics).
 * Resolves to the chosen File list (empty when the user cancels).
 */
export function pickFiles(accept = '.mp3,.wav,.flac', multiple = true): Promise<File[]> {
  return new Promise<File[]>((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = accept
    input.multiple = multiple
    input.onchange = () => {
      const files = Array.from(input.files ?? [])
      resolve(files)
    }
    // The dialog's cancel fires no change event in some browsers — treat a
    // click-then-no-pick as an empty selection via the input going out of scope.
    input.click()
  })
}

/** URL to download a module output file. */
export function downloadUrl(module: string, name: string): string {
  return `${API_BASE}/api/files/download/${module}/${encodeURIComponent(name)}`
}

/**
 * Hand a backend output file to the user: navigate to the download URL — the
 * backend replies `Content-Disposition: attachment`, so the browser saves the
 * file to its Downloads folder.
 *
 * ``path`` may be a full absolute path (e.g. a per-package cut under
 * ``07_output/<stem>/``) or a bare file name; the module-relative portion (everything
 * after the last ``/<module>/``) is used, so nested per-package outputs download
 * correctly. A bare name falls back to its base name.
 */
export function downloadFile(module: string, path: string): void {
  const norm = path.replace(/\\/g, '/')
  const marker = `/${module}/`
  const idx = norm.lastIndexOf(marker)
  const name = idx >= 0 ? norm.slice(idx + marker.length) : (norm.split('/').pop() || path)
  window.location.href = downloadUrl(module, name)
}
