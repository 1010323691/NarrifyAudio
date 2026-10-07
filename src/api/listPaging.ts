export interface ListQuery {
  page: number
  page_size: number
  q: string
  filter: string
}
export interface ListPagination {
  total: number
  page: number
  page_size: number
  counts: Record<string, number>
  reasons?: string[]
  num_pad?: number
}
export function listQuery(options?: Partial<ListQuery>, extra: Record<string, string | number | boolean | undefined> = {}) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries({ ...options, ...extra })) if (value !== undefined) params.set(key, String(value))
  return params.toString()
}
