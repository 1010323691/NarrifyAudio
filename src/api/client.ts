/** Thin HTTP client for the Python backend.

Development requests use Vite's same-origin `/api` proxy. The backend can serve
the production bundle on the same origin; split-origin deployments set
`VITE_API_BASE` and, if customized, `VITE_CSRF_COOKIE_NAME` at build time.
*/
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? ''
const CSRF_COOKIE_NAME: string = import.meta.env.VITE_CSRF_COOKIE_NAME ?? 'narrify_csrf'

// Every request is bounded: a fetch whose connection never settles (a half-dead
// keep-alive slot in the browser's per-origin pool) would otherwise hang the
// router guard — and with it every navigation — forever. 30s comfortably
// covers the slowest JSON endpoint on a LAN while still bounding the stall.
const REQUEST_TIMEOUT_MS = 30_000

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const csrf = typeof document === 'undefined'
    ? ''
    : decodeURIComponent(document.cookie.split('; ').find((item) => item.startsWith(`${CSRF_COOKIE_NAME}=`))?.split('=').slice(1).join('=') || '')
  const isForm = typeof FormData !== 'undefined' && options.body instanceof FormData
  const headers: HeadersInit = { ...(isForm ? {} : { 'Content-Type': 'application/json' }), ...(csrf ? { 'X-CSRF-Token': csrf } : {}), ...(options.headers || {}) }
  // Merge the hard timeout with any caller-supplied signal: whichever fires
  // first aborts the fetch, and the abort reason decides how the error surfaces.
  // Form uploads (manuscript / music-library files) are exempt from the hard
  // timer — a large file on a slow link can legitimately outlast it — but still
  // honour an explicit caller signal.
  const controller = new AbortController()
  let timedOut = false
  const timer: ReturnType<typeof setTimeout> | null = isForm ? null : setTimeout(() => { timedOut = true; controller.abort() }, REQUEST_TIMEOUT_MS)
  const external = options.signal
  const onExternalAbort = () => controller.abort(external?.reason)
  if (external) {
    if (external.aborted) controller.abort(external.reason)
    else external.addEventListener('abort', onExternalAbort, { once: true })
  }
  try {
    const res = await fetch(API_BASE + path, {
      ...options,
      signal: controller.signal,
      credentials: 'include',
      headers,
    })
    const text = await res.text()
    let body: unknown
    try {
      body = text ? JSON.parse(text) : null
    } catch {
      body = text
    }
    if (!res.ok) {
      const detail = (body as any)?.detail ?? (body as any)?.error ?? body ?? res.statusText
      throw new ApiError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
    }
    return body as T
  } catch (error) {
    if (controller.signal.aborted) {
      if (timedOut) throw new ApiError(408, '请求超时，请检查网络后重试')
      const reason = external?.reason
      throw reason instanceof Error ? reason : new DOMException('The operation was aborted.', 'AbortError')
    }
    throw error
  } finally {
    if (timer !== null) clearTimeout(timer)
    external?.removeEventListener('abort', onExternalAbort)
  }
}

export const http = {
  get: <T>(p: string, options?: RequestInit) => request<T>(p, options),
  post: <T>(p: string, body?: unknown, options: RequestInit = {}) =>
    request<T>(p, { ...options, method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) }),
  del: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'DELETE', body: body === undefined ? undefined : JSON.stringify(body) }),
  upload: <T>(p: string, body: FormData) => request<T>(p, { method: 'POST', body }),
}

export function health(): Promise<{ ok: boolean; service: string; port: number }> {
  return http.get('/api/health')
}
