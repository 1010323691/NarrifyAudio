/** Thin HTTP client for the Python backend.

All calls go to the absolute backend origin (CORS is wide open on the backend).
Override the origin with ``VITE_API_BASE`` if the backend ever moves.
*/
export const API_BASE: string = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8642'

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
    : decodeURIComponent(document.cookie.split('; ').find((item) => item.startsWith('narrify_csrf='))?.split('=').slice(1).join('=') || '')
  const res = await fetch(API_BASE + path, {
    ...options,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}), ...(options.headers || {}) },
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
  get: <T>(p: string) => request<T>(p),
  post: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'PATCH', body: body === undefined ? undefined : JSON.stringify(body) }),
  del: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'DELETE', body: body === undefined ? undefined : JSON.stringify(body) }),
}

export function health(): Promise<{ ok: boolean; service: string; port: number }> {
  return http.get('/api/health')
}
