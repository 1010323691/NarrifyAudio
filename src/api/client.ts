/** Thin HTTP client for the Python backend.

Development requests use Vite's same-origin `/api` proxy. The backend can serve
the production bundle on the same origin; split-origin deployments set
`VITE_API_BASE` and, if customized, `VITE_CSRF_COOKIE_NAME` at build time.
*/
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? ''
const CSRF_COOKIE_NAME: string = import.meta.env.VITE_CSRF_COOKIE_NAME ?? 'narrify_csrf'

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
  const res = await fetch(API_BASE + path, {
    ...options,
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
}

export const http = {
  get: <T>(p: string, options?: RequestInit) => request<T>(p, options),
  post: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
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
